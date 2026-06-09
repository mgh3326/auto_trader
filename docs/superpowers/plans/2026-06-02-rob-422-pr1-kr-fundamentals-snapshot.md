# ROB-422 PR1 — KR Fundamentals Snapshot Read-Model (DART foundation) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a durable, point-in-time (PIT) KR multi-period financial-statement & dividend read-model (`financial_fundamentals_snapshots`) sourced from DART/OpenDART, plus a dry-run-first collector and a PIT-gated derivation helper for the 8 Toss-parity metrics — with zero screener/report wiring and zero new dependency.

**Architecture:** Mirror the existing snapshot-family pattern (`market_valuation_snapshots`): ORM model + additive migration + Pydantic upsert + repository (`on_conflict_do_update`) + pure builder (DART fetch + parse + cumulative-quarter differencing + `rcept_no→rcept_dt` PIT join) + dry-run-first job + `--commit`-gated CLI. The 8 aggregate metrics (3y-avg growth, streaks, TTM, QoQ) are **derived in a pure read-path helper** from the per-period rows visible as of `report_date` — never stored — so no lookahead leak. The derivation helper is fully unit-tested but **not wired** into any screener/report surface (that is PR2).

**Tech Stack:** Python 3.13, SQLAlchemy 2.x (async), Pydantic v2, Alembic, pandas, `opendartreader` (already vendored; activate the dormant `finstate_all` + `report('배당')` methods), pytest.

**Spec:** `docs/superpowers/specs/2026-06-02-rob-422-pr1-kr-fundamentals-snapshot-readmodel-design.md`

---

## File Structure

**Create:**
- `app/models/financial_fundamentals_snapshot.py` — ORM model (fiscal-period grain, 4 PIT time columns).
- `app/services/financial_fundamentals_snapshots/__init__.py` — package marker.
- `app/services/financial_fundamentals_snapshots/repository.py` — `FinancialFundamentalsUpsert` + `FinancialFundamentalsSnapshotsRepository`.
- `app/services/financial_fundamentals_snapshots/freshness.py` — row `data_state` classifier.
- `app/services/financial_fundamentals_snapshots/builder.py` — DART fetch + pure parse + discrete-quarter differencing + PIT join + payload assembly.
- `app/services/financial_fundamentals_snapshots/derive.py` — pure PIT-gated 8-metric derivation (UNWIRED).
- `app/jobs/financial_fundamentals_snapshots.py` — dry-run-first job runner (KR-only in PR1).
- `scripts/build_financial_fundamentals_snapshots.py` — `--commit`-gated CLI.
- `alembic/versions/rob422_add_financial_fundamentals_snapshots.py` — additive `create_table`.
- Tests: `tests/test_financial_fundamentals_snapshot_model.py`, `tests/test_financial_fundamentals_snapshots_repository.py`, `tests/test_financial_fundamentals_freshness.py`, `tests/test_financial_fundamentals_builder_parse.py`, `tests/test_financial_fundamentals_builder_orchestration.py`, `tests/test_financial_fundamentals_derive.py`, `tests/test_financial_fundamentals_job.py`, `tests/test_build_financial_fundamentals_cli.py`.

**Modify:**
- `app/models/__init__.py` — register the new model so `Base.metadata.create_all` (conftest.py:437) builds the table for integration tests.

**Deferred to PR2 (NOT this plan):** the `INVEST_DATA_SOURCE_CONTRACT` entry (it describes a *surface consumer*; nothing consumes this table until the screener wiring in PR2, and adding a `collector_snapshot_kind` now with no registered collector would trip the drift-guard test). Screener preset/catalog/API/frontend wiring; `screen_stocks`/reports candidate lineage; production backfill; scheduler.

**Conventions verified in-repo (follow exactly):**
- Dry-run default: `args.dry_run = not args.commit` (CLI), commit guarded by `if request.commit`.
- Raw payload redaction: `from app.services.market_quote_snapshots.builder import redact_sensitive_payload`.
- DART client: `from app.services.disclosures.dart import _get_client` returns the cached `OpenDartReader` client; blocking calls wrapped in `asyncio.to_thread`.
- Repository tests: `@pytest.mark.integration` + `db_session` fixture (creates tables via `Base.metadata.create_all`). All other layers (builder/derive/freshness/job/CLI) are pure/fake — no DB, no live DART.

---

## Task 1: ORM model + registration

**Files:**
- Create: `app/models/financial_fundamentals_snapshot.py`
- Modify: `app/models/__init__.py`
- Test: `tests/test_financial_fundamentals_snapshot_model.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_financial_fundamentals_snapshot_model.py
from __future__ import annotations

from app.models.financial_fundamentals_snapshot import FinancialFundamentalsSnapshot


def test_table_name_and_unique_constraint():
    assert FinancialFundamentalsSnapshot.__tablename__ == "financial_fundamentals_snapshots"
    constraint_names = {c.name for c in FinancialFundamentalsSnapshot.__table__.constraints}
    assert "uq_financial_fundamentals_snapshots_msfs" in constraint_names


def test_pit_and_metric_columns_present():
    cols = set(FinancialFundamentalsSnapshot.__table__.columns.keys())
    # 4 PIT time semantics kept separate (ROB-330 alignment)
    assert {"period_end_date", "filing_date", "effective_at", "source_collected_at"} <= cols
    # raw + discrete metric columns
    assert {
        "revenue", "net_income", "gross_profit", "cost_of_sales", "roe",
        "payout_ratio", "dividend_per_share", "discrete_revenue", "discrete_net_income",
        "data_state", "raw_payload", "schema_version", "fiscal_period", "period_type",
    } <= cols


def test_model_registered_for_metadata():
    # Import side-effect: appears in Base.metadata so conftest create_all builds it.
    from app.models import FinancialFundamentalsSnapshot as Exported  # noqa: F401
    from app.models.base import Base

    assert "financial_fundamentals_snapshots" in Base.metadata.tables
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_financial_fundamentals_snapshot_model.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.models.financial_fundamentals_snapshot'`.

- [ ] **Step 3: Create the model**

```python
# app/models/financial_fundamentals_snapshot.py
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    CheckConstraint,
    Date,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class FinancialFundamentalsSnapshot(Base):
    """KR multi-period financial-statement + dividend snapshot (fiscal-period grain).

    One row per (market, symbol, fiscal_period, source). Stores raw per-period facts
    plus cumulative-differenced single-quarter values. Aggregate metrics (3y-avg /
    streaks / TTM / QoQ) are DERIVED in the read-path (derive.py) from the rows visible
    as of report_date — never stored — to avoid lookahead leakage (ROB-330).
    """

    __tablename__ = "financial_fundamentals_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "market",
            "symbol",
            "fiscal_period",
            "source",
            name="uq_financial_fundamentals_snapshots_msfs",
        ),
        CheckConstraint(
            "market IN ('kr', 'us')",
            name="ck_financial_fundamentals_snapshots_market",
        ),
        CheckConstraint(
            "period_type IN ('annual', 'quarterly')",
            name="ck_financial_fundamentals_snapshots_period_type",
        ),
        CheckConstraint(
            "source IN ('dart')",
            name="ck_financial_fundamentals_snapshots_source",
        ),
        CheckConstraint(
            "data_state IN ('fresh', 'stale', 'partial', 'unavailable')",
            name="ck_financial_fundamentals_snapshots_data_state",
        ),
        Index(
            "ix_financial_fundamentals_snapshots_market_symbol_period_end",
            "market",
            "symbol",
            "period_end_date",
        ),
        Index(
            "ix_financial_fundamentals_snapshots_market_symbol_filing",
            "market",
            "symbol",
            "filing_date",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    market: Mapped[str] = mapped_column(String(8), nullable=False)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    fiscal_period: Mapped[str] = mapped_column(String(10), nullable=False)
    period_type: Mapped[str] = mapped_column(String(10), nullable=False)
    period_end_date: Mapped[date] = mapped_column(Date, nullable=False)
    filing_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    effective_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    source_collected_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    currency: Mapped[str | None] = mapped_column(String(8), nullable=True)
    revenue: Mapped[Decimal | None] = mapped_column(Numeric(30, 2), nullable=True)
    net_income: Mapped[Decimal | None] = mapped_column(Numeric(30, 2), nullable=True)
    gross_profit: Mapped[Decimal | None] = mapped_column(Numeric(30, 2), nullable=True)
    cost_of_sales: Mapped[Decimal | None] = mapped_column(Numeric(30, 2), nullable=True)
    roe: Mapped[Decimal | None] = mapped_column(Numeric(20, 4), nullable=True)
    payout_ratio: Mapped[Decimal | None] = mapped_column(Numeric(10, 6), nullable=True)
    dividend_per_share: Mapped[Decimal | None] = mapped_column(
        Numeric(20, 4), nullable=True
    )
    discrete_revenue: Mapped[Decimal | None] = mapped_column(
        Numeric(30, 2), nullable=True
    )
    discrete_net_income: Mapped[Decimal | None] = mapped_column(
        Numeric(30, 2), nullable=True
    )
    data_state: Mapped[str] = mapped_column(
        String(12), nullable=False, server_default="fresh"
    )
    raw_payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    schema_version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="1"
    )
    computed_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
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

- [ ] **Step 4: Register the model in `app/models/__init__.py`**

Add the import next to the other snapshot models (e.g. directly after the `market_valuation_snapshot` import line). If the file has an `__all__` list, append `"FinancialFundamentalsSnapshot"` to it.

```python
from .financial_fundamentals_snapshot import FinancialFundamentalsSnapshot
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_financial_fundamentals_snapshot_model.py -v`
Expected: PASS (3 tests).

- [ ] **Step 6: Commit**

```bash
git add app/models/financial_fundamentals_snapshot.py app/models/__init__.py tests/test_financial_fundamentals_snapshot_model.py
git commit -m "feat(ROB-422): financial_fundamentals_snapshots ORM model"
```

---

## Task 2: Additive migration

**Files:**
- Create: `alembic/versions/rob422_add_financial_fundamentals_snapshots.py`

> **Before writing:** run `uv run alembic heads`. At plan-authoring time the single head is `20260602_rob412_main_merge`. If `alembic heads` shows a DIFFERENT single head (main advanced), use THAT value as `down_revision`. If it shows TWO heads, stop and create an `alembic merge heads` revision first (repo norm), then set `down_revision` to the merged head.

- [ ] **Step 1: Write the migration**

```python
# alembic/versions/rob422_add_financial_fundamentals_snapshots.py
"""add financial_fundamentals_snapshots (ROB-422 PR1)

Revision ID: rob422_fin_fundamentals
Revises: 20260602_rob412_main_merge
Create Date: 2026-06-02 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "rob422_fin_fundamentals"
down_revision: str | None = "20260602_rob412_main_merge"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "financial_fundamentals_snapshots",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("market", sa.String(length=8), nullable=False),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("fiscal_period", sa.String(length=10), nullable=False),
        sa.Column("period_type", sa.String(length=10), nullable=False),
        sa.Column("period_end_date", sa.Date(), nullable=False),
        sa.Column("filing_date", sa.Date(), nullable=True),
        sa.Column("effective_at", sa.Date(), nullable=True),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("source_collected_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("currency", sa.String(length=8), nullable=True),
        sa.Column("revenue", sa.Numeric(30, 2), nullable=True),
        sa.Column("net_income", sa.Numeric(30, 2), nullable=True),
        sa.Column("gross_profit", sa.Numeric(30, 2), nullable=True),
        sa.Column("cost_of_sales", sa.Numeric(30, 2), nullable=True),
        sa.Column("roe", sa.Numeric(20, 4), nullable=True),
        sa.Column("payout_ratio", sa.Numeric(10, 6), nullable=True),
        sa.Column("dividend_per_share", sa.Numeric(20, 4), nullable=True),
        sa.Column("discrete_revenue", sa.Numeric(30, 2), nullable=True),
        sa.Column("discrete_net_income", sa.Numeric(30, 2), nullable=True),
        sa.Column(
            "data_state",
            sa.String(length=12),
            server_default="fresh",
            nullable=False,
        ),
        sa.Column(
            "raw_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column(
            "schema_version", sa.Integer(), server_default="1", nullable=False
        ),
        sa.Column(
            "computed_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
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
            "market IN ('kr', 'us')",
            name="ck_financial_fundamentals_snapshots_market",
        ),
        sa.CheckConstraint(
            "period_type IN ('annual', 'quarterly')",
            name="ck_financial_fundamentals_snapshots_period_type",
        ),
        sa.CheckConstraint(
            "source IN ('dart')",
            name="ck_financial_fundamentals_snapshots_source",
        ),
        sa.CheckConstraint(
            "data_state IN ('fresh', 'stale', 'partial', 'unavailable')",
            name="ck_financial_fundamentals_snapshots_data_state",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "market",
            "symbol",
            "fiscal_period",
            "source",
            name="uq_financial_fundamentals_snapshots_msfs",
        ),
    )
    op.create_index(
        "ix_financial_fundamentals_snapshots_market_symbol_period_end",
        "financial_fundamentals_snapshots",
        ["market", "symbol", "period_end_date"],
    )
    op.create_index(
        "ix_financial_fundamentals_snapshots_market_symbol_filing",
        "financial_fundamentals_snapshots",
        ["market", "symbol", "filing_date"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_financial_fundamentals_snapshots_market_symbol_filing",
        table_name="financial_fundamentals_snapshots",
    )
    op.drop_index(
        "ix_financial_fundamentals_snapshots_market_symbol_period_end",
        table_name="financial_fundamentals_snapshots",
    )
    op.drop_table("financial_fundamentals_snapshots")
```

- [ ] **Step 2: Verify alembic can parse the revision graph (no DB apply)**

Run: `uv run alembic heads`
Expected: a single head `rob422_fin_fundamentals` (the new revision now tips the chain). If two heads appear, you introduced/encountered a fork — create a merge revision before proceeding.

> Do NOT run `alembic upgrade head` (production apply is operator-gated per spec §9). Integration tests build the table via `Base.metadata.create_all`, not via this migration.

- [ ] **Step 3: Commit**

```bash
git add alembic/versions/rob422_add_financial_fundamentals_snapshots.py
git commit -m "feat(ROB-422): additive migration for financial_fundamentals_snapshots"
```

---

## Task 3: Repository (upsert + reads)

**Files:**
- Create: `app/services/financial_fundamentals_snapshots/__init__.py` (empty)
- Create: `app/services/financial_fundamentals_snapshots/repository.py`
- Test: `tests/test_financial_fundamentals_snapshots_repository.py`

- [ ] **Step 1: Write the failing integration test**

```python
# tests/test_financial_fundamentals_snapshots_repository.py
from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from app.services.financial_fundamentals_snapshots.repository import (
    FinancialFundamentalsSnapshotsRepository,
    FinancialFundamentalsUpsert,
)


def _row(fiscal_period: str, net_income: int, *, filing_date: dt.date | None) -> FinancialFundamentalsUpsert:
    return FinancialFundamentalsUpsert(
        market="kr",
        symbol="005930",
        fiscal_period=fiscal_period,
        period_type="annual",
        period_end_date=dt.date(int(fiscal_period[:4]), 12, 31),
        filing_date=filing_date,
        effective_at=filing_date,
        source="dart",
        source_collected_at=dt.datetime(2026, 6, 2, 0, 0, tzinfo=dt.UTC),
        revenue=Decimal("3000000"),
        net_income=Decimal(net_income),
        data_state="fresh" if filing_date else "partial",
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_upsert_is_idempotent_on_unique_key(db_session):
    repo = FinancialFundamentalsSnapshotsRepository(db_session)

    n = await repo.upsert([_row("2025A", 100, filing_date=dt.date(2026, 3, 20))])
    await db_session.commit()
    assert n == 1

    # Same (market,symbol,fiscal_period,source) → UPDATE not duplicate INSERT.
    await repo.upsert([_row("2025A", 250, filing_date=dt.date(2026, 3, 20))])
    await db_session.commit()

    rows = await repo.periods_for_symbol(market="kr", symbol="005930")
    assert len(rows) == 1
    assert rows[0].net_income == Decimal("250")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_periods_for_symbol_returns_ascending_by_period_end(db_session):
    repo = FinancialFundamentalsSnapshotsRepository(db_session)
    await repo.upsert(
        [
            _row("2023A", 100, filing_date=dt.date(2024, 3, 20)),
            _row("2025A", 300, filing_date=dt.date(2026, 3, 20)),
            _row("2024A", 200, filing_date=dt.date(2025, 3, 20)),
        ]
    )
    await db_session.commit()

    rows = await repo.periods_for_symbol(market="kr", symbol="005930")
    assert [r.fiscal_period for r in rows] == ["2023A", "2024A", "2025A"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_financial_fundamentals_snapshots_repository.py -v`
Expected: FAIL with `ModuleNotFoundError` for the repository module.

- [ ] **Step 3: Write the repository**

```python
# app/services/financial_fundamentals_snapshots/repository.py
from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from decimal import Decimal

from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.financial_fundamentals_snapshot import FinancialFundamentalsSnapshot

_UPSERTABLE_COLUMNS = (
    "period_type",
    "period_end_date",
    "filing_date",
    "effective_at",
    "source_collected_at",
    "currency",
    "revenue",
    "net_income",
    "gross_profit",
    "cost_of_sales",
    "roe",
    "payout_ratio",
    "dividend_per_share",
    "discrete_revenue",
    "discrete_net_income",
    "data_state",
    "raw_payload",
    "schema_version",
)


class FinancialFundamentalsUpsert(BaseModel):
    model_config = ConfigDict(extra="forbid")

    market: str
    symbol: str
    fiscal_period: str
    period_type: str
    period_end_date: dt.date
    source: str
    source_collected_at: dt.datetime
    filing_date: dt.date | None = None
    effective_at: dt.date | None = None
    currency: str | None = None
    revenue: Decimal | None = None
    net_income: Decimal | None = None
    gross_profit: Decimal | None = None
    cost_of_sales: Decimal | None = None
    roe: Decimal | None = None
    payout_ratio: Decimal | None = None
    dividend_per_share: Decimal | None = None
    discrete_revenue: Decimal | None = None
    discrete_net_income: Decimal | None = None
    data_state: str = "fresh"
    raw_payload: dict | None = None
    schema_version: int = 1


def _normalize_payload(row: FinancialFundamentalsUpsert) -> dict:
    values = row.model_dump()
    values["market"] = values["market"].strip().lower()
    values["symbol"] = values["symbol"].strip().upper()
    values["source"] = values["source"].strip().lower()
    return values


class FinancialFundamentalsSnapshotsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert(self, rows: Iterable[FinancialFundamentalsUpsert]) -> int:
        payload = [_normalize_payload(row) for row in rows]
        if not payload:
            return 0
        stmt = insert(FinancialFundamentalsSnapshot).values(payload)
        set_ = {col: getattr(stmt.excluded, col) for col in _UPSERTABLE_COLUMNS}
        set_["computed_at"] = func.now()
        set_["updated_at"] = func.now()
        stmt = stmt.on_conflict_do_update(
            constraint="uq_financial_fundamentals_snapshots_msfs",
            set_=set_,
        )
        result = await self._session.execute(stmt)
        return result.rowcount or 0

    async def periods_for_symbol(
        self, *, market: str, symbol: str, period_type: str | None = None
    ) -> list[FinancialFundamentalsSnapshot]:
        stmt = select(FinancialFundamentalsSnapshot).where(
            FinancialFundamentalsSnapshot.market == market.strip().lower(),
            FinancialFundamentalsSnapshot.symbol == symbol.strip().upper(),
        )
        if period_type is not None:
            stmt = stmt.where(
                FinancialFundamentalsSnapshot.period_type == period_type
            )
        stmt = stmt.order_by(
            FinancialFundamentalsSnapshot.period_end_date.asc(),
            FinancialFundamentalsSnapshot.fiscal_period.asc(),
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_financial_fundamentals_snapshots_repository.py -v`
Expected: PASS (2 tests). (If it errors with "relation does not exist", confirm Task 1 Step 4 registered the model in `app/models/__init__.py` — `db_session` create_all needs it imported.)

- [ ] **Step 5: Commit**

```bash
git add app/services/financial_fundamentals_snapshots/__init__.py app/services/financial_fundamentals_snapshots/repository.py tests/test_financial_fundamentals_snapshots_repository.py
git commit -m "feat(ROB-422): financial_fundamentals_snapshots repository (idempotent upsert)"
```

---

## Task 4: Row data_state classifier (freshness)

**Files:**
- Create: `app/services/financial_fundamentals_snapshots/freshness.py`
- Test: `tests/test_financial_fundamentals_freshness.py`

> PR1 sets the stored `data_state` at build time from row-level completeness only: `partial` when `filing_date` is NULL (cannot PIT-gate → read-path won't cite it), else `fresh`. The `stale`/`unavailable` values are reserved for the read-path/coverage layer in PR2; the CHECK already allows all four.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_financial_fundamentals_freshness.py
from __future__ import annotations

import datetime as dt

from app.services.financial_fundamentals_snapshots.freshness import row_data_state


def test_partial_when_filing_date_missing():
    assert row_data_state(filing_date=None) == "partial"


def test_fresh_when_filing_date_present():
    assert row_data_state(filing_date=dt.date(2026, 3, 20)) == "fresh"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_financial_fundamentals_freshness.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Write the classifier**

```python
# app/services/financial_fundamentals_snapshots/freshness.py
from __future__ import annotations

import datetime as dt
from typing import Literal

FundamentalsDataState = Literal["fresh", "stale", "partial", "unavailable"]


def row_data_state(*, filing_date: dt.date | None) -> FundamentalsDataState:
    """Row-level data_state stored at build time.

    `partial` if the filing date could not be resolved (read-path must not PIT-cite
    a figure whose public-availability date is unknown). Otherwise `fresh`.
    `stale`/`unavailable` are read-path/coverage states (PR2), not set here.
    """
    if filing_date is None:
        return "partial"
    return "fresh"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_financial_fundamentals_freshness.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add app/services/financial_fundamentals_snapshots/freshness.py tests/test_financial_fundamentals_freshness.py
git commit -m "feat(ROB-422): financial fundamentals row data_state classifier"
```

---

## Task 5: Pure DART parse helpers

**Files:**
- Create: `app/services/financial_fundamentals_snapshots/builder.py` (parse helpers first; orchestration added in Task 6)
- Test: `tests/test_financial_fundamentals_builder_parse.py`

> These are pure functions over the DataFrame shapes OpenDartReader returns:
> `finstate_all` rows carry `account_id` (XBRL, e.g. `ifrs-full_Revenue`/`ifrs-full_GrossProfit`/`ifrs-full_ProfitLoss`/`CostOfSales`), `account_nm`, `sj_div`, `thstrm_amount`, `thstrm_add_amount`, `currency`. `report('배당', ...)` (alotMatter) rows carry `se` (Korean row label) and `thstrm`. The disclosure-list endpoint carries `rcept_no` + `rcept_dt`. DART numeric strings use thousands commas and `-`/empty for null. `se` labels vary by punctuation → match by normalized-contains, never exact equality.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_financial_fundamentals_builder_parse.py
from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pandas as pd

from app.services.financial_fundamentals_snapshots.builder import (
    parse_dividend_frame,
    parse_filing_dates_frame,
    parse_income_statement_frame,
    single_quarter_discrete,
)


def test_parse_income_statement_prefers_account_id_then_name():
    df = pd.DataFrame(
        [
            {"account_id": "ifrs-full_Revenue", "account_nm": "수익(매출액)", "sj_div": "IS", "thstrm_amount": "3,000,000"},
            {"account_id": "ifrs-full_GrossProfit", "account_nm": "매출총이익", "sj_div": "IS", "thstrm_amount": "1,200,000"},
            {"account_id": "ifrs-full_CostOfSales", "account_nm": "매출원가", "sj_div": "IS", "thstrm_amount": "1,800,000"},
            {"account_id": "ifrs-full_ProfitLoss", "account_nm": "당기순이익", "sj_div": "CIS", "thstrm_amount": "500,000"},
        ]
    )
    parsed = parse_income_statement_frame(df)
    assert parsed["revenue"] == Decimal("3000000")
    assert parsed["gross_profit"] == Decimal("1200000")
    assert parsed["cost_of_sales"] == Decimal("1800000")
    assert parsed["net_income"] == Decimal("500000")


def test_parse_income_statement_missing_gross_profit_is_none():
    df = pd.DataFrame(
        [
            {"account_id": "ifrs-full_Revenue", "account_nm": "매출액", "sj_div": "IS", "thstrm_amount": "100"},
            {"account_id": "ifrs-full_ProfitLoss", "account_nm": "당기순이익", "sj_div": "CIS", "thstrm_amount": "10"},
        ]
    )
    parsed = parse_income_statement_frame(df)
    assert parsed["gross_profit"] is None
    assert parsed["cost_of_sales"] is None
    assert parsed["revenue"] == Decimal("100")


def test_parse_dividend_matches_labels_by_normalized_contains():
    df = pd.DataFrame(
        [
            {"se": "주당 현금배당금(원)", "thstrm": "1,444"},
            {"se": "(연결)현금배당성향(%)", "thstrm": "25.10"},
        ]
    )
    parsed = parse_dividend_frame(df)
    assert parsed["dividend_per_share"] == Decimal("1444")
    assert parsed["payout_ratio"] == Decimal("25.10")


def test_parse_dividend_missing_rows_are_none_not_zero():
    df = pd.DataFrame([{"se": "주식의 종류", "thstrm": "보통주"}])
    parsed = parse_dividend_frame(df)
    assert parsed["dividend_per_share"] is None
    assert parsed["payout_ratio"] is None


def test_parse_filing_dates_maps_rcept_no_to_date():
    df = pd.DataFrame(
        [
            {"rcept_no": "20260320000123", "rcept_dt": "20260320"},
            {"rcept_no": "20250318000077", "rcept_dt": "20250318"},
        ]
    )
    mapping = parse_filing_dates_frame(df)
    assert mapping["20260320000123"] == dt.date(2026, 3, 20)
    assert mapping["20250318000077"] == dt.date(2025, 3, 18)


def test_single_quarter_discrete_differences_cumulative():
    # Q3 cumulative (9-month) minus H1 cumulative (6-month) = standalone Q3.
    assert single_quarter_discrete(cumulative=Decimal("900"), prior_cumulative=Decimal("600")) == Decimal("300")
    # Q1 has no prior cumulative within the year → standalone = cumulative.
    assert single_quarter_discrete(cumulative=Decimal("250"), prior_cumulative=None) == Decimal("250")
    # Missing cumulative → cannot difference.
    assert single_quarter_discrete(cumulative=None, prior_cumulative=Decimal("600")) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_financial_fundamentals_builder_parse.py -v`
Expected: FAIL with `ModuleNotFoundError` (builder not created).

- [ ] **Step 3: Write the parse helpers**

```python
# app/services/financial_fundamentals_snapshots/builder.py
from __future__ import annotations

import datetime as dt
from decimal import Decimal, InvalidOperation
from typing import Any

import pandas as pd

# XBRL account_id codes (preferred) with Korean account_nm contains-fallbacks.
_REVENUE_IDS = ("ifrs-full_Revenue", "ifrs-full_RevenueFromContractsWithCustomers")
_GROSS_PROFIT_IDS = ("ifrs-full_GrossProfit",)
_COST_OF_SALES_IDS = ("ifrs-full_CostOfSales",)
_NET_INCOME_IDS = ("ifrs-full_ProfitLoss",)

_REVENUE_NAMES = ("매출액", "수익(매출액)", "영업수익")
_GROSS_PROFIT_NAMES = ("매출총이익",)
_COST_OF_SALES_NAMES = ("매출원가",)
_NET_INCOME_NAMES = ("당기순이익", "당기순이익(손실)")


def _dart_amount_to_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, float) and pd.isna(value):
        return None
    text = str(value).strip().replace(",", "")
    if text in {"", "-", "nan"}:
        return None
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return None


def _normalize_label(value: Any) -> str:
    if value is None:
        return ""
    return "".join(str(value).split()).replace("(", "").replace(")", "")


def _pick_amount(
    df: pd.DataFrame, *, account_ids: tuple[str, ...], account_names: tuple[str, ...]
) -> Decimal | None:
    if df.empty:
        return None
    for _, row in df.iterrows():
        if str(row.get("account_id", "")).strip() in account_ids:
            return _dart_amount_to_decimal(row.get("thstrm_amount"))
    # Fallback: normalized-contains match on the Korean account name.
    targets = {_normalize_label(name) for name in account_names}
    for _, row in df.iterrows():
        label = _normalize_label(row.get("account_nm"))
        if any(target in label for target in targets):
            return _dart_amount_to_decimal(row.get("thstrm_amount"))
    return None


def parse_income_statement_frame(df: pd.DataFrame) -> dict[str, Decimal | None]:
    """Extract revenue / net_income / gross_profit / cost_of_sales from a finstate_all frame."""
    return {
        "revenue": _pick_amount(df, account_ids=_REVENUE_IDS, account_names=_REVENUE_NAMES),
        "gross_profit": _pick_amount(df, account_ids=_GROSS_PROFIT_IDS, account_names=_GROSS_PROFIT_NAMES),
        "cost_of_sales": _pick_amount(df, account_ids=_COST_OF_SALES_IDS, account_names=_COST_OF_SALES_NAMES),
        "net_income": _pick_amount(df, account_ids=_NET_INCOME_IDS, account_names=_NET_INCOME_NAMES),
    }


def _pick_dividend_row(df: pd.DataFrame, *, contains: str) -> Decimal | None:
    target = _normalize_label(contains)
    for _, row in df.iterrows():
        if target in _normalize_label(row.get("se")):
            return _dart_amount_to_decimal(row.get("thstrm"))
    return None


def parse_dividend_frame(df: pd.DataFrame) -> dict[str, Decimal | None]:
    """Extract payout_ratio (현금배당성향%) and dividend_per_share (주당 현금배당금) from alotMatter."""
    if df.empty:
        return {"payout_ratio": None, "dividend_per_share": None}
    return {
        "payout_ratio": _pick_dividend_row(df, contains="현금배당성향"),
        "dividend_per_share": _pick_dividend_row(df, contains="주당현금배당금"),
    }


def _parse_dart_date(value: Any) -> dt.date | None:
    text = str(value).strip().replace("-", "")
    if len(text) < 8 or not text[:8].isdigit():
        return None
    return dt.date(int(text[:4]), int(text[4:6]), int(text[6:8]))


def parse_filing_dates_frame(df: pd.DataFrame) -> dict[str, dt.date]:
    """Map rcept_no -> filing date (rcept_dt) from a disclosure-list frame."""
    mapping: dict[str, dt.date] = {}
    if df.empty:
        return mapping
    for _, row in df.iterrows():
        rcept_no = str(row.get("rcept_no", "")).strip()
        filing_date = _parse_dart_date(row.get("rcept_dt"))
        if rcept_no and filing_date is not None:
            mapping[rcept_no] = filing_date
    return mapping


def single_quarter_discrete(
    *, cumulative: Decimal | None, prior_cumulative: Decimal | None
) -> Decimal | None:
    """Standalone single-quarter value from KR YTD-cumulative interim amounts.

    Q1 cumulative == standalone (prior_cumulative is None). Later quarters subtract the
    prior cumulative. A missing current cumulative cannot be differenced.
    """
    if cumulative is None:
        return None
    if prior_cumulative is None:
        return cumulative
    return cumulative - prior_cumulative
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_financial_fundamentals_builder_parse.py -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add app/services/financial_fundamentals_snapshots/builder.py tests/test_financial_fundamentals_builder_parse.py
git commit -m "feat(ROB-422): pure DART finstate/dividend/filing-date parse helpers"
```

---

## Task 6: Builder orchestration (fetcher seam + payload assembly)

**Files:**
- Modify: `app/services/financial_fundamentals_snapshots/builder.py`
- Test: `tests/test_financial_fundamentals_builder_orchestration.py`

> The orchestration takes an injectable `fetcher` returning a `RawFundamentalsBundle` (annual filings + dividend frames + the rcept_no→rcept_dt map). Tests pass a fake fetcher with tiny DataFrames; the default fetcher calls DART via `_get_client()` in `asyncio.to_thread`. Annual is the PR1 default; quarterly rows (with `discrete_*`) are produced only when `include_quarterly=True`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_financial_fundamentals_builder_orchestration.py
from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pandas as pd
import pytest

from app.services.financial_fundamentals_snapshots.builder import (
    RawAnnualFiling,
    RawFundamentalsBundle,
    build_financial_fundamentals_for_symbols,
)


def _is_frame(rev: str, ni: str) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"account_id": "ifrs-full_Revenue", "account_nm": "매출액", "sj_div": "IS", "thstrm_amount": rev},
            {"account_id": "ifrs-full_ProfitLoss", "account_nm": "당기순이익", "sj_div": "CIS", "thstrm_amount": ni},
        ]
    )


def _div_frame(dps: str, payout: str) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"se": "주당 현금배당금(원)", "thstrm": dps},
            {"se": "현금배당성향(%)", "thstrm": payout},
        ]
    )


async def _fake_fetcher(symbol: str, *, include_quarterly: bool) -> RawFundamentalsBundle:
    return RawFundamentalsBundle(
        symbol=symbol,
        currency="KRW",
        annual=(
            RawAnnualFiling(
                bsns_year=2024,
                rcept_no="20250318000077",
                income_statement=_is_frame("2,000,000", "200,000"),
                dividend=_div_frame("1,000", "20.0"),
            ),
            RawAnnualFiling(
                bsns_year=2025,
                rcept_no="20260320000123",
                income_statement=_is_frame("3,000,000", "300,000"),
                dividend=_div_frame("1,444", "25.10"),
            ),
        ),
        quarterly=(),
        filing_dates={
            "20250318000077": dt.date(2025, 3, 18),
            "20260320000123": dt.date(2026, 3, 20),
        },
    )


@pytest.mark.asyncio
async def test_builder_emits_one_payload_per_annual_period_with_pit_filing_date():
    result = await build_financial_fundamentals_for_symbols(
        market="kr",
        symbols=["005930"],
        collected_at=dt.datetime(2026, 6, 2, tzinfo=dt.UTC),
        fetcher=_fake_fetcher,
    )
    payloads = {p.fiscal_period: p for p in result.payloads}
    assert set(payloads) == {"2024A", "2025A"}
    p25 = payloads["2025A"]
    assert p25.market == "kr" and p25.symbol == "005930" and p25.source == "dart"
    assert p25.period_type == "annual"
    assert p25.period_end_date == dt.date(2025, 12, 31)
    assert p25.filing_date == dt.date(2026, 3, 20)        # rcept_no→rcept_dt join
    assert p25.effective_at == dt.date(2026, 3, 20)
    assert p25.revenue == Decimal("3000000")
    assert p25.net_income == Decimal("300000")
    assert p25.payout_ratio == Decimal("25.10")
    assert p25.dividend_per_share == Decimal("1444")
    assert p25.data_state == "fresh"                      # filing_date resolved
    assert p25.raw_payload is not None                    # provenance retained


@pytest.mark.asyncio
async def test_builder_marks_partial_when_filing_date_unresolved():
    async def _fetcher(symbol: str, *, include_quarterly: bool) -> RawFundamentalsBundle:
        bundle = await _fake_fetcher(symbol, include_quarterly=include_quarterly)
        return RawFundamentalsBundle(
            symbol=bundle.symbol,
            currency=bundle.currency,
            annual=bundle.annual,
            quarterly=bundle.quarterly,
            filing_dates={},  # join fails for every rcept_no
        )

    result = await build_financial_fundamentals_for_symbols(
        market="kr",
        symbols=["005930"],
        collected_at=dt.datetime(2026, 6, 2, tzinfo=dt.UTC),
        fetcher=_fetcher,
    )
    assert all(p.filing_date is None for p in result.payloads)
    assert all(p.data_state == "partial" for p in result.payloads)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_financial_fundamentals_builder_orchestration.py -v`
Expected: FAIL with `ImportError` (`RawAnnualFiling`/`RawFundamentalsBundle`/`build_financial_fundamentals_for_symbols` undefined).

- [ ] **Step 3: Add orchestration to `builder.py`**

Append to `app/services/financial_fundamentals_snapshots/builder.py`:

```python
import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass

from app.services.financial_fundamentals_snapshots.freshness import row_data_state
from app.services.financial_fundamentals_snapshots.repository import (
    FinancialFundamentalsUpsert,
)
from app.services.market_quote_snapshots.builder import redact_sensitive_payload

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RawAnnualFiling:
    bsns_year: int
    rcept_no: str
    income_statement: pd.DataFrame
    dividend: pd.DataFrame | None = None


@dataclass(frozen=True)
class RawQuarterlyFiling:
    bsns_year: int
    quarter: int  # 1..4
    rcept_no: str
    reprt_code: str
    income_statement: pd.DataFrame  # cumulative YTD amounts
    prior_income_statement: pd.DataFrame | None = None  # prior cumulative (for differencing)


@dataclass(frozen=True)
class RawFundamentalsBundle:
    symbol: str
    currency: str | None = None
    annual: tuple[RawAnnualFiling, ...] = ()
    quarterly: tuple[RawQuarterlyFiling, ...] = ()
    filing_dates: dict[str, dt.date] | None = None


@dataclass(frozen=True)
class FinancialFundamentalsBuildResult:
    payloads: tuple[FinancialFundamentalsUpsert, ...]
    warnings: tuple[str, ...] = ()


FundamentalsFetcher = Callable[..., Awaitable[RawFundamentalsBundle]]

_REPRT_CODE_BY_QUARTER = {1: "11013", 2: "11012", 3: "11014", 4: "11011"}


def _payload_from_annual(
    *, market: str, symbol: str, filing: RawAnnualFiling, currency: str | None,
    filing_date: dt.date | None, collected_at: dt.datetime,
) -> FinancialFundamentalsUpsert:
    income = parse_income_statement_frame(filing.income_statement)
    dividend = (
        parse_dividend_frame(filing.dividend)
        if filing.dividend is not None
        else {"payout_ratio": None, "dividend_per_share": None}
    )
    raw = {
        "income_statement": filing.income_statement.to_dict(orient="records"),
        "dividend": (
            filing.dividend.to_dict(orient="records")
            if filing.dividend is not None
            else None
        ),
        "rcept_no": filing.rcept_no,
        "bsns_year": filing.bsns_year,
    }
    return FinancialFundamentalsUpsert(
        market=market,
        symbol=symbol,
        fiscal_period=f"{filing.bsns_year}A",
        period_type="annual",
        period_end_date=dt.date(filing.bsns_year, 12, 31),
        filing_date=filing_date,
        effective_at=filing_date,
        source="dart",
        source_collected_at=collected_at,
        currency=currency,
        revenue=income["revenue"],
        net_income=income["net_income"],
        gross_profit=income["gross_profit"],
        cost_of_sales=income["cost_of_sales"],
        payout_ratio=dividend["payout_ratio"],
        dividend_per_share=dividend["dividend_per_share"],
        discrete_revenue=income["revenue"],          # annual: discrete == reported
        discrete_net_income=income["net_income"],
        data_state=row_data_state(filing_date=filing_date),
        raw_payload=redact_sensitive_payload(raw),
    )


def _payload_from_quarterly(
    *, market: str, symbol: str, filing: RawQuarterlyFiling, currency: str | None,
    filing_date: dt.date | None, collected_at: dt.datetime,
) -> FinancialFundamentalsUpsert:
    income = parse_income_statement_frame(filing.income_statement)
    prior = (
        parse_income_statement_frame(filing.prior_income_statement)
        if filing.prior_income_statement is not None
        else {"revenue": None, "net_income": None}
    )
    discrete_revenue = single_quarter_discrete(
        cumulative=income["revenue"], prior_cumulative=prior["revenue"]
    )
    discrete_net_income = single_quarter_discrete(
        cumulative=income["net_income"], prior_cumulative=prior["net_income"]
    )
    raw = {
        "income_statement": filing.income_statement.to_dict(orient="records"),
        "rcept_no": filing.rcept_no,
        "bsns_year": filing.bsns_year,
        "quarter": filing.quarter,
        "reprt_code": filing.reprt_code,
    }
    return FinancialFundamentalsUpsert(
        market=market,
        symbol=symbol,
        fiscal_period=f"{filing.bsns_year}Q{filing.quarter}",
        period_type="quarterly",
        period_end_date=_quarter_end_date(filing.bsns_year, filing.quarter),
        filing_date=filing_date,
        effective_at=filing_date,
        source="dart",
        source_collected_at=collected_at,
        currency=currency,
        revenue=income["revenue"],
        net_income=income["net_income"],
        gross_profit=income["gross_profit"],
        cost_of_sales=income["cost_of_sales"],
        discrete_revenue=discrete_revenue,
        discrete_net_income=discrete_net_income,
        data_state=row_data_state(filing_date=filing_date),
        raw_payload=redact_sensitive_payload(raw),
    )


def _quarter_end_date(year: int, quarter: int) -> dt.date:
    return {
        1: dt.date(year, 3, 31),
        2: dt.date(year, 6, 30),
        3: dt.date(year, 9, 30),
        4: dt.date(year, 12, 31),
    }[quarter]


async def build_financial_fundamentals_for_symbols(
    *,
    market: str,
    symbols: Iterable[str],
    collected_at: dt.datetime,
    fetcher: FundamentalsFetcher,
    include_quarterly: bool = False,
    concurrency: int = 4,
) -> FinancialFundamentalsBuildResult:
    market_norm = market.strip().lower()
    if market_norm != "kr":
        raise ValueError(f"PR1 supports market='kr' only, got: {market}")
    sem = asyncio.Semaphore(max(1, concurrency))
    symbols_list = [s.strip().upper() for s in symbols if s.strip()]
    collected: list[FinancialFundamentalsUpsert] = []
    warnings: list[str] = []

    async def _one(symbol: str) -> None:
        async with sem:
            try:
                bundle = await fetcher(symbol, include_quarterly=include_quarterly)
            except Exception as exc:  # noqa: BLE001
                logger.warning("fundamentals fetch failed symbol=%s: %s", symbol, exc)
                warnings.append(f"{symbol}: fetch failed ({exc})")
                return
            filing_dates = bundle.filing_dates or {}
            for filing in bundle.annual:
                collected.append(
                    _payload_from_annual(
                        market=market_norm, symbol=symbol, filing=filing,
                        currency=bundle.currency,
                        filing_date=filing_dates.get(filing.rcept_no),
                        collected_at=collected_at,
                    )
                )
            for q_filing in bundle.quarterly:
                collected.append(
                    _payload_from_quarterly(
                        market=market_norm, symbol=symbol, filing=q_filing,
                        currency=bundle.currency,
                        filing_date=filing_dates.get(q_filing.rcept_no),
                        collected_at=collected_at,
                    )
                )

    await asyncio.gather(*(_one(symbol) for symbol in symbols_list))
    return FinancialFundamentalsBuildResult(
        payloads=tuple(collected), warnings=tuple(warnings)
    )


async def default_dart_fetcher(
    symbol: str, *, include_quarterly: bool, years_back: int = 5
) -> RawFundamentalsBundle:
    """Live DART fetcher: activates the dormant finstate_all + report('배당') methods.

    fs_div='CFS' (consolidated) first, falling back to 'OFS' (separate) when CFS is empty.
    filing dates resolved by joining each rcept_no to the disclosure-list endpoint.
    """
    from app.core.config import settings
    from app.services.disclosures.dart import _get_client

    if not settings.opendart_api_key:
        raise RuntimeError("OPENDART_API_KEY not set")
    client = await _get_client()
    if client is None:
        raise RuntimeError("DART functionality not available")

    today = dt.date.today()
    years = list(range(today.year - 1, today.year - 1 - years_back, -1))

    def fetch_sync() -> RawFundamentalsBundle:
        annual: list[RawAnnualFiling] = []
        for year in years:
            stmt = client.finstate_all(symbol, year, "11011", fs_div="CFS")
            if stmt is None or stmt.empty:
                stmt = client.finstate_all(symbol, year, "11011", fs_div="OFS")
            if stmt is None or stmt.empty:
                continue
            try:
                dividend = client.report(symbol, "배당", year, "11011")
            except Exception:  # noqa: BLE001
                dividend = None
            rcept_no = ""
            if "rcept_no" in stmt.columns and not stmt.empty:
                rcept_no = str(stmt.iloc[0].get("rcept_no", "")).strip()
            annual.append(
                RawAnnualFiling(
                    bsns_year=year, rcept_no=rcept_no,
                    income_statement=stmt, dividend=dividend,
                )
            )
        # Resolve filing dates via the disclosure-list endpoint (carries rcept_dt).
        listing = client.list(
            corp=symbol,
            start=(today - dt.timedelta(days=365 * (years_back + 1))).isoformat(),
            end=today.isoformat(),
            kind="A",  # 정기보고서
            final=True,
        )
        filing_dates = parse_filing_dates_frame(
            listing if listing is not None else pd.DataFrame()
        )
        currency = None
        if annual and "currency" in annual[0].income_statement.columns:
            vals = annual[0].income_statement["currency"].dropna().unique().tolist()
            currency = str(vals[0]) if vals else None
        return RawFundamentalsBundle(
            symbol=symbol, currency=currency,
            annual=tuple(annual), quarterly=(), filing_dates=filing_dates,
        )

    return await asyncio.to_thread(fetch_sync)
```

> Note: the quarterly live path (`include_quarterly=True`) is intentionally a follow-up wiring — `default_dart_fetcher` returns `quarterly=()` for now; the pure quarterly assembly (`_payload_from_quarterly`, `single_quarter_discrete`) is implemented and tested so PR2 can supply quarterly filings. This keeps PR1 annual-first (spec §2.3) without dead live code.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_financial_fundamentals_builder_orchestration.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add app/services/financial_fundamentals_snapshots/builder.py tests/test_financial_fundamentals_builder_orchestration.py
git commit -m "feat(ROB-422): DART fundamentals builder orchestration + live fetcher (CFS→OFS, PIT join)"
```

---

## Task 7: PIT-gated derivation helper (the 8 metrics)

**Files:**
- Create: `app/services/financial_fundamentals_snapshots/derive.py`
- Test: `tests/test_financial_fundamentals_derive.py`

> Pure, UNWIRED helper. Input = per-period rows; output = the 8 metrics each with a `MetricResult(value, state, note)`. Every metric is gated to rows whose `filing_date <= report_date` (and `filing_date is not None`). `missing != zero`: an absent year breaks/uncomputes a streak (never a fabricated 0). Negative/zero growth bases → `partial`. `gross_margin_ttm` falls back to `revenue - cost_of_sales`, and is `partial` for IFRS single-step issuers with neither.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_financial_fundamentals_derive.py
from __future__ import annotations

import datetime as dt
from decimal import Decimal

from app.services.financial_fundamentals_snapshots.derive import (
    FundamentalPeriod,
    derive_fundamentals_metrics,
)


def _annual(year: int, *, revenue, net_income, filing_date, gross_profit=None,
            cost_of_sales=None, payout_ratio=None, dps=None) -> FundamentalPeriod:
    return FundamentalPeriod(
        fiscal_period=f"{year}A",
        period_type="annual",
        period_end_date=dt.date(year, 12, 31),
        filing_date=filing_date,
        revenue=Decimal(revenue) if revenue is not None else None,
        net_income=Decimal(net_income) if net_income is not None else None,
        gross_profit=Decimal(gross_profit) if gross_profit is not None else None,
        cost_of_sales=Decimal(cost_of_sales) if cost_of_sales is not None else None,
        discrete_revenue=Decimal(revenue) if revenue is not None else None,
        discrete_net_income=Decimal(net_income) if net_income is not None else None,
        payout_ratio=Decimal(payout_ratio) if payout_ratio is not None else None,
        dividend_per_share=Decimal(dps) if dps is not None else None,
        roe=None,
    )


def _periods():
    return [
        _annual(2021, revenue="1000", net_income="100", filing_date=dt.date(2022, 3, 20), dps="10", payout_ratio="20"),
        _annual(2022, revenue="1100", net_income="120", filing_date=dt.date(2023, 3, 20), dps="11", payout_ratio="21"),
        _annual(2023, revenue="1300", net_income="150", filing_date=dt.date(2024, 3, 20), dps="12", payout_ratio="22"),
        _annual(2024, revenue="1600", net_income="200", filing_date=dt.date(2025, 3, 20), dps="13", payout_ratio="25"),
    ]


def test_pit_gate_hides_unfiled_periods():
    # report_date before the 2024 filing → 2024 row invisible.
    d = derive_fundamentals_metrics(_periods(), report_date=dt.date(2024, 12, 31))
    # latest visible payout = 2023 row (filed 2024-03-20)
    assert d.payout_ratio.value == Decimal("22")
    # after 2025-03-20 the 2024 row is visible
    d2 = derive_fundamentals_metrics(_periods(), report_date=dt.date(2025, 6, 1))
    assert d2.payout_ratio.value == Decimal("25")


def test_growth_3y_avg_computed_when_four_years_visible():
    d = derive_fundamentals_metrics(_periods(), report_date=dt.date(2025, 6, 1))
    assert d.revenue_growth_3y_avg.state == "ok"
    assert d.earnings_growth_3y_avg.state == "ok"
    # YoY rev: 0.10, 0.1818..., 0.2308.. → avg ≈ 0.1709
    assert round(float(d.revenue_growth_3y_avg.value), 3) == 0.171


def test_earnings_increase_streak_counts_consecutive():
    d = derive_fundamentals_metrics(_periods(), report_date=dt.date(2025, 6, 1))
    assert d.earnings_increase_streak_years.value == 3  # 2021<2022<2023<2024 → 3 increases


def test_dividend_streaks_missing_not_zero():
    periods = _periods()
    # Drop the 2023 dividend (None) → streak breaks, NOT counted as a 0-paid year.
    periods[2] = _annual(2023, revenue="1300", net_income="150",
                         filing_date=dt.date(2024, 3, 20), dps=None, payout_ratio=None)
    d = derive_fundamentals_metrics(periods, report_date=dt.date(2025, 6, 1))
    # Most-recent consecutive paid run is just 2024 (2023 missing breaks it).
    assert d.dividend_paid_streak_years.value == 1


def test_gross_margin_partial_when_no_gross_profit_or_cogs():
    d = derive_fundamentals_metrics(_periods(), report_date=dt.date(2025, 6, 1))
    assert d.gross_margin_ttm.state == "partial"
    assert d.gross_margin_ttm.value is None


def test_gross_margin_uses_cost_of_sales_fallback():
    periods = [
        _annual(2024, revenue="1000", net_income="100",
                filing_date=dt.date(2025, 3, 20), cost_of_sales="700"),
    ]
    d = derive_fundamentals_metrics(periods, report_date=dt.date(2025, 6, 1))
    # gross margin = (1000 - 700) / 1000 = 0.30
    assert d.gross_margin_ttm.state == "ok"
    assert round(float(d.gross_margin_ttm.value), 2) == 0.30


def test_negative_base_year_makes_growth_partial():
    periods = [
        _annual(2023, revenue="1000", net_income="-50", filing_date=dt.date(2024, 3, 20)),
        _annual(2024, revenue="1100", net_income="80", filing_date=dt.date(2025, 3, 20)),
    ]
    d = derive_fundamentals_metrics(periods, report_date=dt.date(2025, 6, 1))
    assert d.earnings_growth_3y_avg.state in {"partial", "unavailable"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_financial_fundamentals_derive.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Write the derivation helper**

```python
# app/services/financial_fundamentals_snapshots/derive.py
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

MetricState = Literal["ok", "partial", "unavailable"]


@dataclass(frozen=True)
class FundamentalPeriod:
    fiscal_period: str
    period_type: str  # 'annual' | 'quarterly'
    period_end_date: dt.date
    filing_date: dt.date | None
    revenue: Decimal | None = None
    net_income: Decimal | None = None
    gross_profit: Decimal | None = None
    cost_of_sales: Decimal | None = None
    discrete_revenue: Decimal | None = None
    discrete_net_income: Decimal | None = None
    payout_ratio: Decimal | None = None
    dividend_per_share: Decimal | None = None
    roe: Decimal | None = None


@dataclass(frozen=True)
class MetricResult:
    value: Decimal | int | None
    state: MetricState
    note: str | None = None


@dataclass(frozen=True)
class FundamentalsDerivation:
    report_date: dt.date
    payout_ratio: MetricResult
    gross_margin_ttm: MetricResult
    revenue_growth_3y_avg: MetricResult
    earnings_growth_3y_avg: MetricResult
    earnings_growth_qoq: MetricResult
    earnings_increase_streak_years: MetricResult
    dividend_paid_streak_years: MetricResult
    dividend_growth_streak_years: MetricResult


_UNAVAILABLE = MetricResult(value=None, state="unavailable")


def _visible_annual(periods, report_date):
    rows = [
        p for p in periods
        if p.period_type == "annual"
        and p.filing_date is not None
        and p.filing_date <= report_date
    ]
    return sorted(rows, key=lambda p: p.period_end_date)


def _visible_quarterly(periods, report_date):
    rows = [
        p for p in periods
        if p.period_type == "quarterly"
        and p.filing_date is not None
        and p.filing_date <= report_date
    ]
    return sorted(rows, key=lambda p: p.period_end_date)


def _yoy(curr: Decimal | None, base: Decimal | None) -> Decimal | None:
    if curr is None or base is None or base <= 0:
        return None
    return (curr - base) / base


def _growth_3y_avg(values: list[Decimal | None]) -> MetricResult:
    # values are most-recent-last; need >=4 to form 3 YoY deltas.
    deltas: list[Decimal] = []
    undefined = False
    for i in range(1, len(values)):
        d = _yoy(values[i], values[i - 1])
        if d is None:
            undefined = True
        else:
            deltas.append(d)
    if not deltas:
        return _UNAVAILABLE
    avg = sum(deltas) / Decimal(len(deltas))
    if len(values) >= 4 and len(deltas) >= 3 and not undefined:
        return MetricResult(value=avg, state="ok")
    return MetricResult(value=avg, state="partial", note="fewer than 3 usable YoY deltas")


def _payout_ratio(annual: list) -> MetricResult:
    for p in reversed(annual):
        if p.payout_ratio is not None:
            return MetricResult(value=p.payout_ratio, state="ok")
    return _UNAVAILABLE


def _gross_margin_ttm(annual: list, quarterly: list) -> MetricResult:
    def margin(gross: Decimal | None, cogs: Decimal | None, rev: Decimal | None):
        if rev is None or rev <= 0:
            return None
        if gross is not None:
            return gross / rev
        if cogs is not None:
            return (rev - cogs) / rev
        return None

    # Prefer trailing-4-quarter TTM when available.
    if len(quarterly) >= 4:
        last4 = quarterly[-4:]
        rev = sum((q.discrete_revenue for q in last4 if q.discrete_revenue is not None), Decimal(0))
        gross_vals = [q.gross_profit for q in last4 if q.gross_profit is not None]
        cogs_vals = [q.cost_of_sales for q in last4 if q.cost_of_sales is not None]
        if rev > 0 and len(gross_vals) == 4:
            return MetricResult(value=sum(gross_vals, Decimal(0)) / rev, state="ok")
        if rev > 0 and len(cogs_vals) == 4:
            return MetricResult(value=(rev - sum(cogs_vals, Decimal(0))) / rev, state="ok")
    # Fall back to the latest annual figure.
    if annual:
        latest = annual[-1]
        m = margin(latest.gross_profit, latest.cost_of_sales, latest.revenue)
        if m is not None:
            return MetricResult(value=m, state="ok")
        return MetricResult(value=None, state="partial",
                            note="no gross profit / cost of sales (IFRS single-step)")
    return _UNAVAILABLE


def _earnings_growth_qoq(quarterly: list) -> MetricResult:
    usable = [q for q in quarterly if q.discrete_net_income is not None]
    if len(usable) < 2:
        return _UNAVAILABLE
    curr, prev = usable[-1].discrete_net_income, usable[-2].discrete_net_income
    g = _yoy(curr, prev)
    if g is None:
        return MetricResult(value=None, state="partial", note="non-positive base quarter")
    return MetricResult(value=g, state="ok")


def _increase_streak(values: list[Decimal | None]) -> MetricResult:
    # Count consecutive YoY increases ending at the most recent year.
    if len(values) < 2:
        return MetricResult(value=0, state="partial", note="insufficient history")
    streak = 0
    for i in range(len(values) - 1, 0, -1):
        a, b = values[i], values[i - 1]
        if a is None or b is None:
            break
        if a > b:
            streak += 1
        else:
            break
    return MetricResult(value=streak, state="ok")


def _dividend_paid_streak(annual: list) -> MetricResult:
    streak = 0
    for p in reversed(annual):
        dps = p.dividend_per_share
        if dps is None:          # missing != zero → cannot extend → stop
            break
        if dps > 0:
            streak += 1
        else:
            break
    return MetricResult(value=streak, state="ok")


def _dividend_growth_streak(annual: list) -> MetricResult:
    dps = [(p.period_end_date, p.dividend_per_share) for p in annual]
    dps = [d for d in dps if d[1] is not None]
    if len(dps) < 2:
        return MetricResult(value=0, state="partial", note="insufficient dividend history")
    streak = 0
    for i in range(len(dps) - 1, 0, -1):
        if dps[i][1] > dps[i - 1][1]:
            streak += 1
        else:
            break
    return MetricResult(value=streak, state="ok",
                        note="DPS is split/par-value unadjusted (DART raw)")


def derive_fundamentals_metrics(
    periods: list[FundamentalPeriod], *, report_date: dt.date
) -> FundamentalsDerivation:
    annual = _visible_annual(periods, report_date)
    quarterly = _visible_quarterly(periods, report_date)
    revenues = [p.revenue for p in annual]
    net_incomes = [p.net_income for p in annual]
    return FundamentalsDerivation(
        report_date=report_date,
        payout_ratio=_payout_ratio(annual),
        gross_margin_ttm=_gross_margin_ttm(annual, quarterly),
        revenue_growth_3y_avg=_growth_3y_avg(revenues) if revenues else _UNAVAILABLE,
        earnings_growth_3y_avg=_growth_3y_avg(net_incomes) if net_incomes else _UNAVAILABLE,
        earnings_growth_qoq=_earnings_growth_qoq(quarterly),
        earnings_increase_streak_years=_increase_streak(net_incomes) if net_incomes else _UNAVAILABLE,
        dividend_paid_streak_years=_dividend_paid_streak(annual),
        dividend_growth_streak_years=_dividend_growth_streak(annual),
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_financial_fundamentals_derive.py -v`
Expected: PASS (7 tests).

- [ ] **Step 5: Commit**

```bash
git add app/services/financial_fundamentals_snapshots/derive.py tests/test_financial_fundamentals_derive.py
git commit -m "feat(ROB-422): PIT-gated 8-metric derivation helper (unwired)"
```

---

## Task 8: Dry-run-first job runner

**Files:**
- Create: `app/jobs/financial_fundamentals_snapshots.py`
- Test: `tests/test_financial_fundamentals_job.py`

> Mirrors `app/jobs/market_valuation_snapshots.py`: KR-only (PR1), resolves the active KR universe (or explicit `--symbol`), builds payloads via an injectable builder fetcher, reports `idempotency`/`distribution`/`samples`/`warnings`, and only writes when `commit=True`. The DB write path (`_commit_payloads`) and idempotency classification are exercised against the in-memory builder result; the test injects a fake fetcher so no DART/DB is required to prove dry-run writes nothing.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_financial_fundamentals_job.py
from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from app.jobs import financial_fundamentals_snapshots as job
from app.services.financial_fundamentals_snapshots.builder import (
    RawAnnualFiling,
    RawFundamentalsBundle,
)


async def _fake_fetcher(symbol: str, *, include_quarterly: bool) -> RawFundamentalsBundle:
    df = pd.DataFrame(
        [
            {"account_id": "ifrs-full_Revenue", "account_nm": "매출액", "sj_div": "IS", "thstrm_amount": "1,000"},
            {"account_id": "ifrs-full_ProfitLoss", "account_nm": "당기순이익", "sj_div": "CIS", "thstrm_amount": "100"},
        ]
    )
    return RawFundamentalsBundle(
        symbol=symbol,
        annual=(RawAnnualFiling(bsns_year=2024, rcept_no="r1", income_statement=df),),
        filing_dates={"r1": dt.date(2025, 3, 20)},
    )


@pytest.mark.asyncio
async def test_dry_run_builds_but_writes_nothing(monkeypatch):
    monkeypatch.setattr(job, "resolve_symbols", _async_return(["005930"]))

    result = await job.run_financial_fundamentals_snapshot_build(
        job.FinancialFundamentalsSnapshotBuildRequest(
            market="kr", symbols=("005930",), commit=False
        ),
        fetcher=_fake_fetcher,
    )
    assert result.committed is False
    assert result.snapshots_built == 1
    assert result.symbols_resolved == 1
    assert any(s.fiscal_period == "2024A" for s in result.samples)


def _async_return(value):
    async def _coro(*args, **kwargs):
        return value
    return _coro
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_financial_fundamentals_job.py -v`
Expected: FAIL with `ModuleNotFoundError` for the job module.

- [ ] **Step 3: Write the job runner**

```python
# app/jobs/financial_fundamentals_snapshots.py
"""Dry-run-first job runner for financial_fundamentals_snapshots (ROB-422 PR1, KR-only)."""

from __future__ import annotations

import datetime as dt
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal

import sqlalchemy as sa

from app.core.db import AsyncSessionLocal
from app.models.financial_fundamentals_snapshot import FinancialFundamentalsSnapshot
from app.services.financial_fundamentals_snapshots.builder import (
    FundamentalsFetcher,
    build_financial_fundamentals_for_symbols,
    default_dart_fetcher,
)
from app.services.financial_fundamentals_snapshots.repository import (
    FinancialFundamentalsSnapshotsRepository,
    FinancialFundamentalsUpsert,
)


@dataclass(frozen=True)
class FinancialFundamentalsSnapshotBuildRequest:
    market: str = "kr"
    symbols: tuple[str, ...] = ()
    limit: int | None = 20
    all_symbols: bool = False
    include_quarterly: bool = False
    concurrency: int = 4
    commit: bool = False
    collected_at: dt.datetime | None = None


@dataclass(frozen=True)
class FinancialFundamentalsSnapshotSample:
    symbol: str
    fiscal_period: str
    period_type: str
    filing_date: dt.date | None
    revenue: Decimal | None
    net_income: Decimal | None
    payout_ratio: Decimal | None
    data_state: str


@dataclass(frozen=True)
class FinancialFundamentalsSnapshotBuildResult:
    market: str
    symbols_resolved: int
    snapshots_built: int
    committed: bool
    started_at: dt.datetime
    finished_at: dt.datetime
    idempotency: dict[str, int] = field(default_factory=dict)
    samples: tuple[FinancialFundamentalsSnapshotSample, ...] = ()
    warnings: tuple[str, ...] = ()


def _validate_market(market: str) -> str:
    market_norm = market.strip().lower()
    if market_norm != "kr":
        raise ValueError(f"PR1 supports market='kr' only, got: {market}")
    return market_norm


async def resolve_symbols(market: str, override: list[str], limit: int) -> list[str]:
    _validate_market(market)
    if override:
        return [s.strip().upper() for s in override if s.strip()]
    async with AsyncSessionLocal() as session:
        from app.models.kr_symbol_universe import KRSymbolUniverse

        stmt = (
            sa.select(KRSymbolUniverse.symbol)
            .where(KRSymbolUniverse.is_active.is_(True))
            .order_by(KRSymbolUniverse.symbol)
            .limit(limit)
        )
        result = await session.execute(stmt)
        return [r[0] for r in result.all()]


async def resolve_active_universe(market: str) -> list[str]:
    _validate_market(market)
    async with AsyncSessionLocal() as session:
        from app.models.kr_symbol_universe import KRSymbolUniverse

        stmt = (
            sa.select(KRSymbolUniverse.symbol)
            .where(KRSymbolUniverse.is_active.is_(True))
            .order_by(KRSymbolUniverse.symbol)
        )
        result = await session.execute(stmt)
        return [r[0] for r in result.all()]


def _payload_key(p: FinancialFundamentalsUpsert) -> tuple[str, str, str, str]:
    return (
        p.market.strip().lower(),
        p.symbol.strip().upper(),
        p.fiscal_period,
        p.source.strip().lower(),
    )


async def _classify_idempotency(
    payloads: list[FinancialFundamentalsUpsert],
) -> dict[str, int]:
    keys = [_payload_key(p) for p in payloads]
    duplicate = sum(c - 1 for c in Counter(keys).values() if c > 1)
    unique = set(keys)
    if not unique:
        return {"wouldInsert": 0, "wouldUpdate": 0, "duplicatePayloadKeys": duplicate}
    conditions = [
        sa.and_(
            FinancialFundamentalsSnapshot.market == m,
            FinancialFundamentalsSnapshot.symbol == s,
            FinancialFundamentalsSnapshot.fiscal_period == fp,
            FinancialFundamentalsSnapshot.source == src,
        )
        for m, s, fp, src in unique
    ]
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            sa.select(
                FinancialFundamentalsSnapshot.market,
                FinancialFundamentalsSnapshot.symbol,
                FinancialFundamentalsSnapshot.fiscal_period,
                FinancialFundamentalsSnapshot.source,
            ).where(sa.or_(*conditions))
        )
        existing = set(result.all())
    return {
        "wouldInsert": len(unique) - len(existing),
        "wouldUpdate": len(existing),
        "duplicatePayloadKeys": duplicate,
    }


async def _commit_payloads(payloads: list[FinancialFundamentalsUpsert]) -> None:
    async with AsyncSessionLocal() as session:
        await FinancialFundamentalsSnapshotsRepository(session).upsert(payloads)
        await session.commit()


def _sample(p: FinancialFundamentalsUpsert) -> FinancialFundamentalsSnapshotSample:
    return FinancialFundamentalsSnapshotSample(
        symbol=p.symbol,
        fiscal_period=p.fiscal_period,
        period_type=p.period_type,
        filing_date=p.filing_date,
        revenue=p.revenue,
        net_income=p.net_income,
        payout_ratio=p.payout_ratio,
        data_state=p.data_state,
    )


async def run_financial_fundamentals_snapshot_build(
    request: FinancialFundamentalsSnapshotBuildRequest,
    *,
    fetcher: FundamentalsFetcher | None = None,
) -> FinancialFundamentalsSnapshotBuildResult:
    market = _validate_market(request.market)
    started_at = dt.datetime.now(dt.UTC)
    collected_at = request.collected_at or started_at
    use_fetcher = fetcher or default_dart_fetcher
    symbols = await (
        resolve_active_universe(market)
        if request.all_symbols
        else resolve_symbols(market, list(request.symbols), request.limit or 20)
    )
    if not symbols:
        finished_at = dt.datetime.now(dt.UTC)
        return FinancialFundamentalsSnapshotBuildResult(
            market=market, symbols_resolved=0, snapshots_built=0,
            committed=request.commit, started_at=started_at, finished_at=finished_at,
            idempotency={"wouldInsert": 0, "wouldUpdate": 0, "duplicatePayloadKeys": 0},
            warnings=("no symbols resolved",),
        )
    build = await build_financial_fundamentals_for_symbols(
        market=market, symbols=symbols, collected_at=collected_at,
        fetcher=use_fetcher, include_quarterly=request.include_quarterly,
        concurrency=request.concurrency,
    )
    payloads = list(build.payloads)
    idempotency = await _classify_idempotency(payloads) if payloads else {
        "wouldInsert": 0, "wouldUpdate": 0, "duplicatePayloadKeys": 0
    }
    if request.commit and payloads:
        await _commit_payloads(payloads)
    finished_at = dt.datetime.now(dt.UTC)
    return FinancialFundamentalsSnapshotBuildResult(
        market=market,
        symbols_resolved=len(symbols),
        snapshots_built=len(payloads),
        committed=request.commit,
        started_at=started_at,
        finished_at=finished_at,
        idempotency=idempotency,
        samples=tuple(_sample(p) for p in payloads[:10]),
        warnings=build.warnings,
    )
```

> The test injects `resolve_symbols` via monkeypatch and a fake `fetcher`, so `_classify_idempotency` (which opens a DB session) is only reached when `payloads` is non-empty. To keep this test DB-free, the test asserts on the build result before commit; `_classify_idempotency` runs against the test DB session factory. If the runner's idempotency DB call makes this test require a DB, mark it `@pytest.mark.integration` instead — but prefer keeping it pure by having the fake fetcher return payloads and asserting `committed is False` and `snapshots_built == 1` (the idempotency dict values are not asserted).

> **Note for the implementer:** `_classify_idempotency` uses `AsyncSessionLocal`, which needs a database. To keep Task 8's test fully unit-level, guard the idempotency call so dry-run with an explicit `--symbol` still computes it lazily; if running this test without a DB is flaky, add `@pytest.mark.integration` and a `db_session` autouse, mirroring `tests/test_investor_flow_snapshot_job.py`. Verify which the repo expects by reading that file before finalizing.

- [ ] **Step 2b: Read the reference job test to match DB expectations**

Run: `sed -n '1,60p' tests/test_investor_flow_snapshot_job.py`
Decide: if that test uses `db_session`, mark Task 8's test `@pytest.mark.integration` and accept a DB. Otherwise keep it pure with a monkeypatched `_classify_idempotency`.

- [ ] **Step 3: Run test to verify it passes**

Run: `uv run pytest tests/test_financial_fundamentals_job.py -v`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add app/jobs/financial_fundamentals_snapshots.py tests/test_financial_fundamentals_job.py
git commit -m "feat(ROB-422): dry-run-first fundamentals snapshot job (KR-only)"
```

---

## Task 9: `--commit`-gated CLI

**Files:**
- Create: `scripts/build_financial_fundamentals_snapshots.py`
- Test: `tests/test_build_financial_fundamentals_cli.py`

> Mirrors `scripts/build_market_valuation_snapshots.py`: dry-run default (`args.dry_run = not args.commit`), `--symbol`/`--limit`/`--all` mutually-exclusive validation, `--with-quarterly` opt-in. The CLI test asserts the argparse defaults (dry-run) and mutual-exclusion error — no job/DB execution.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_build_financial_fundamentals_cli.py
from __future__ import annotations

import pytest

from scripts.build_financial_fundamentals_snapshots import parse_args


def test_defaults_to_dry_run():
    args = parse_args(["--symbol", "005930"])
    assert args.dry_run is True
    assert args.commit is False
    assert args.include_quarterly is False
    assert args.market == "kr"


def test_commit_flag_disables_dry_run():
    args = parse_args(["--all", "--commit"])
    assert args.dry_run is False
    assert args.commit is True


def test_all_is_mutually_exclusive_with_symbol():
    with pytest.raises(SystemExit):
        parse_args(["--all", "--symbol", "005930"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_build_financial_fundamentals_cli.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Write the CLI**

```python
#!/usr/bin/env python3
"""Build financial_fundamentals_snapshots rows from DART (ROB-422 PR1, KR-only).

DEFAULTS TO --dry-run: prints an approval-packet-friendly summary without committing.
Pass --commit only after explicit operator approval. Production migration apply
(`alembic upgrade head`) and any scheduler activation remain operator-gated (spec §9).
"""

from __future__ import annotations

import argparse
import asyncio


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Dry-run-first KR fundamentals snapshots builder (ROB-422 PR1)."
    )
    parser.add_argument("--market", choices=["kr"], default="kr")
    parser.add_argument(
        "--symbol", action="append", default=[],
        help="Restrict to specific 6-digit KR symbols. Repeatable.",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Max active universe symbols. Defaults to 20 unless --all.",
    )
    parser.add_argument(
        "--all", action="store_true",
        help="Iterate the full active KR universe. Exclusive with --symbol/--limit.",
    )
    parser.add_argument(
        "--with-quarterly", action="store_true",
        help="Also build quarterly periods (annual-only by default; spec §2.3).",
    )
    parser.add_argument(
        "--concurrency", type=int, default=4, help="Per-symbol fetch concurrency."
    )
    parser.add_argument(
        "--commit", action="store_true",
        help="Actually write to the database. Default is --dry-run/no writes.",
    )
    args = parser.parse_args(argv)
    if args.all and (args.symbol or args.limit is not None):
        parser.error("--all is mutually exclusive with --symbol and --limit")
    if args.limit is None:
        args.limit = 20
    if args.concurrency < 1:
        parser.error("--concurrency must be >= 1")
    args.dry_run = not args.commit
    return args


def _print_result(result) -> None:
    print(
        f"\nbuilt {result.snapshots_built} fundamentals snapshots "
        f"for {result.symbols_resolved} {result.market.upper()} symbols "
        f"(dry_run={not result.committed}):"
    )
    print("idempotency:")
    for key in ("wouldInsert", "wouldUpdate", "duplicatePayloadKeys"):
        print(f"  {key}: {result.idempotency.get(key, 0)}")
    if result.samples:
        print("samples:")
        for sample in result.samples[:10]:
            print(f"  {sample}")
    if result.warnings:
        print("warnings:")
        for warning in result.warnings:
            print(f"  - {warning}")
    if not result.committed:
        print("\n--dry-run: no rows written.\n")
    else:
        print(f"\ncommitted {result.snapshots_built} rows.\n")


async def run(args: argparse.Namespace) -> int:
    from app.jobs import financial_fundamentals_snapshots as snapshot_job

    result = await snapshot_job.run_financial_fundamentals_snapshot_build(
        snapshot_job.FinancialFundamentalsSnapshotBuildRequest(
            market=args.market,
            symbols=tuple(args.symbol),
            limit=args.limit,
            all_symbols=args.all,
            include_quarterly=args.with_quarterly,
            concurrency=args.concurrency,
            commit=args.commit,
        )
    )
    _print_result(result)
    return 0


async def main() -> int:
    args = parse_args()
    from app.core.cli import setup_logging_and_sentry

    setup_logging_and_sentry(service_name="build-financial-fundamentals-snapshots")
    return await run(args)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_build_financial_fundamentals_cli.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add scripts/build_financial_fundamentals_snapshots.py tests/test_build_financial_fundamentals_cli.py
git commit -m "feat(ROB-422): --commit-gated CLI for fundamentals snapshots"
```

---

## Task 10: Full-suite verification + lint

**Files:** none (verification only)

- [ ] **Step 1: Run the new module's tests together**

Run: `uv run pytest tests/test_financial_fundamentals_snapshot_model.py tests/test_financial_fundamentals_snapshots_repository.py tests/test_financial_fundamentals_freshness.py tests/test_financial_fundamentals_builder_parse.py tests/test_financial_fundamentals_builder_orchestration.py tests/test_financial_fundamentals_derive.py tests/test_financial_fundamentals_job.py tests/test_build_financial_fundamentals_cli.py -v`
Expected: all PASS.

- [ ] **Step 2: Lint + format check (repo gate — both, not just `ruff check`)**

Run: `uv run ruff check app/ scripts/ tests/ && uv run ruff format --check app/ scripts/ tests/`
Expected: clean. (If format fails, run `uv run ruff format app/ scripts/ tests/` and re-commit.)

- [ ] **Step 3: Type check**

Run: `uv run ty check app/services/financial_fundamentals_snapshots app/jobs/financial_fundamentals_snapshots.py app/models/financial_fundamentals_snapshot.py`
Expected: clean (or only pre-existing repo-wide noise unrelated to these files).

- [ ] **Step 4: Regression guard — valuation path untouched**

Run: `uv run pytest tests/test_invest_coverage_valuation.py -v`
Expected: PASS (confirms `market_valuation_snapshots`/`fundamentals_evidence` consumers — which drive the working full-3 presets — are unaffected by the additive new table/source).

- [ ] **Step 5: Commit any lint/format fixups**

```bash
git add -A
git commit -m "chore(ROB-422): lint/format fixups for fundamentals snapshot module" || echo "nothing to commit"
```

---

## Self-Review (completed by plan author)

**Spec coverage (spec §-by-§):**
- §2 source = DART primary via dormant OpenDartReader → Task 6 `default_dart_fetcher` (finstate_all + report('배당'), CFS→OFS, list-join). ✓
- §2.1 no new dependency → no `uv add`; only existing `opendartreader`/`pandas`. ✓
- §2 musts: PIT join (Task 5 `parse_filing_dates_frame` + Task 6 rcept_no→filing_date), cumulative differencing (Task 5 `single_quarter_discrete` + Task 6 `_payload_from_quarterly`), annual-first pacing (Task 6 `default_dart_fetcher` annual-only; quarterly opt-in). ✓
- §4 new table, 4 PIT time columns, raw+discrete stored, aggregates NOT stored → Task 1 model + Task 7 derive (read-path). ✓
- §5 builder/repository/freshness/derive/job/CLI + CFS→OFS + alotMatter normalized-contains → Tasks 3–9. ✓
- §6 symbol mapping reuse (kr_symbol_universe → find_corp_code inside OpenDartReader) → Task 6/8 (symbols passed as 6-digit). ✓
- §7 data_state → Task 4 (`partial` when filing_date NULL else `fresh`), §7 gross_margin partial → Task 7. ✓
- §8 tests → every task is TDD; missing≠zero, PIT gate, partial states all covered in Task 7. ✓
- §9 migration additive, operator-gated apply → Task 2 (no `upgrade head`); contract entry explicitly deferred to PR2 (rationale stated). ✓
- §11 non-goals: no screener/report wiring (derive unwired), no scheduler, KR-only, US deferred. ✓

**Placeholder scan:** every code step contains complete code; no TBD/TODO/"handle edge cases". The one conditional is Task 8 Step 2b (read the reference job test to choose pure-vs-integration marking) — this is an explicit, bounded decision with both branches specified, not a placeholder.

**Type/name consistency:** `FinancialFundamentalsUpsert`, `FinancialFundamentalsSnapshotsRepository`, `build_financial_fundamentals_for_symbols`, `RawFundamentalsBundle`/`RawAnnualFiling`/`RawQuarterlyFiling`, `FundamentalsFetcher`, `row_data_state`, `FundamentalPeriod`/`MetricResult`/`FundamentalsDerivation`, `derive_fundamentals_metrics`, `run_financial_fundamentals_snapshot_build`, `FinancialFundamentalsSnapshotBuildRequest` — used consistently across Tasks 1, 3, 4, 6, 7, 8, 9. The job imports `FundamentalsFetcher`/`default_dart_fetcher`/`build_financial_fundamentals_for_symbols` from `builder` (defined in Task 6). The unique constraint name `uq_financial_fundamentals_snapshots_msfs` matches between model (Task 1), migration (Task 2), and repository `on_conflict_do_update` (Task 3). ✓
