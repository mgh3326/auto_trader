# Trade Review System — DB Migration Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Create DB schema for trade review system (execution records, indicator snapshots, post-trade reviews, pending order monitoring) and remove unused legacy DCA tables.

**Architecture:** New `review` schema with 4 tables, following the existing `research` schema pattern established in `app/models/research_backtest.py` and `alembic/versions/f2c1e9b7a4d0_add_research_backtest_tables.py`. Single Alembic migration handles both legacy table removal and new schema creation.

**Tech Stack:** SQLAlchemy 2.0 (mapped_column), Alembic, PostgreSQL

---

## Spec Deviation: `user_trade_defaults` is NOT safe to drop

**The original spec says to drop `user_trade_defaults` (claiming 0 rows, no model). This is incorrect.**

Active references found:
- **Model:** `UserTradeDefaults` in `app/models/symbol_trade_settings.py:27`
- **Service:** `UserTradeDefaultsService` in `app/services/symbol_trade_settings_service.py`
- **API:** GET/PUT `/user-defaults` in `app/routers/symbol_settings.py`
- **Tests:** `tests/test_symbol_trade_settings.py`

**Decision: Do NOT drop `user_trade_defaults`.** Only drop `dca_plans` and `dca_plan_steps`.

---

## Reference Files (read before implementing)

| Purpose | File |
|---------|------|
| Schema pattern to copy | `app/models/research_backtest.py` |
| Migration pattern to copy | `alembic/versions/f2c1e9b7a4d0_add_research_backtest_tables.py` |
| InstrumentType enum | `app/models/trading.py:19-24` |
| CheckConstraint patterns | `app/models/trade_profile.py:62-94` |
| Existing model exports | `app/models/__init__.py` |
| Alembic env config | `alembic/env.py` |
| Current Alembic head | `86961c84a0ce` (merge migration) |

---

### Task 1: Create review models

**Files:**
- Create: `app/models/review.py`

**Step 1: Create `app/models/review.py`**

```python
"""Trade review system models (review schema)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    CheckConstraint,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.models.base import Base
from app.models.trading import InstrumentType


# ---------------------------------------------------------------------------
# review.trades — executed trade records
# ---------------------------------------------------------------------------
class Trade(Base):
    __tablename__ = "trades"
    __table_args__ = (
        UniqueConstraint("account", "order_id", name="uq_review_trades_account_order"),
        CheckConstraint("side IN ('buy','sell')", name="review_trades_side"),
        CheckConstraint("currency IN ('KRW','USD')", name="review_trades_currency"),
        Index("ix_review_trades_trade_date", "trade_date"),
        Index("ix_review_trades_symbol", "symbol"),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    trade_date: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    instrument_type: Mapped[InstrumentType] = mapped_column(
        Enum(InstrumentType, name="instrument_type", create_type=False), nullable=False
    )
    side: Mapped[str] = mapped_column(Text, nullable=False)
    price: Mapped[float] = mapped_column(Numeric(20, 4), nullable=False)
    quantity: Mapped[float] = mapped_column(Numeric(20, 8), nullable=False)
    total_amount: Mapped[float] = mapped_column(Numeric(20, 4), nullable=False)
    fee: Mapped[float] = mapped_column(Numeric(20, 4), nullable=False, default=0)
    currency: Mapped[str] = mapped_column(Text, nullable=False, default="KRW")
    account: Mapped[str] = mapped_column(Text, nullable=False)
    order_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )


# ---------------------------------------------------------------------------
# review.trade_snapshots — indicator snapshot at execution time
# ---------------------------------------------------------------------------
class TradeSnapshot(Base):
    __tablename__ = "trade_snapshots"
    __table_args__ = (
        UniqueConstraint("trade_id", name="uq_review_trade_snapshots_trade_id"),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    trade_id: Mapped[int] = mapped_column(
        ForeignKey("review.trades.id", ondelete="CASCADE"), nullable=False
    )
    rsi_14: Mapped[float | None] = mapped_column(Numeric(6, 2))
    rsi_7: Mapped[float | None] = mapped_column(Numeric(6, 2))
    ema_20: Mapped[float | None] = mapped_column(Numeric(20, 4))
    ema_200: Mapped[float | None] = mapped_column(Numeric(20, 4))
    macd: Mapped[float | None] = mapped_column(Numeric(20, 4))
    macd_signal: Mapped[float | None] = mapped_column(Numeric(20, 4))
    adx: Mapped[float | None] = mapped_column(Numeric(6, 2))
    stoch_rsi_k: Mapped[float | None] = mapped_column(Numeric(6, 2))
    volume_ratio: Mapped[float | None] = mapped_column(Numeric(10, 2))
    fear_greed: Mapped[int | None] = mapped_column(SmallInteger)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )


# ---------------------------------------------------------------------------
# review.trade_reviews — post-trade evaluation
# ---------------------------------------------------------------------------
class TradeReview(Base):
    __tablename__ = "trade_reviews"
    __table_args__ = (
        CheckConstraint(
            "verdict IN ('good','neutral','bad')", name="review_trade_reviews_verdict"
        ),
        CheckConstraint(
            "review_type IN ('daily','weekly','monthly','manual')",
            name="review_trade_reviews_review_type",
        ),
        Index("ix_review_trade_reviews_trade_type", "trade_id", "review_type"),
        Index("ix_review_trade_reviews_review_date", "review_date"),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    trade_id: Mapped[int] = mapped_column(
        ForeignKey("review.trades.id", ondelete="CASCADE"), nullable=False
    )
    review_date: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    price_at_review: Mapped[float | None] = mapped_column(Numeric(20, 4))
    pnl_pct: Mapped[float | None] = mapped_column(Numeric(8, 4))
    verdict: Mapped[str] = mapped_column(Text, nullable=False)
    comment: Mapped[str | None] = mapped_column(Text)
    review_type: Mapped[str] = mapped_column(Text, nullable=False, default="daily")
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )


# ---------------------------------------------------------------------------
# review.pending_snapshots — unfilled order monitoring
# ---------------------------------------------------------------------------
class PendingSnapshot(Base):
    __tablename__ = "pending_snapshots"
    __table_args__ = (
        CheckConstraint("side IN ('buy','sell')", name="review_pending_side"),
        CheckConstraint(
            "resolved_as IN ('pending','filled','cancelled','expired')",
            name="review_pending_resolved_as",
        ),
        Index(
            "ix_review_pending_resolved_date", "resolved_as", "snapshot_date"
        ),
        Index(
            "ix_review_pending_account_order_date",
            "account",
            "order_id",
            "snapshot_date",
        ),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    snapshot_date: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    instrument_type: Mapped[InstrumentType] = mapped_column(
        Enum(InstrumentType, name="instrument_type", create_type=False), nullable=False
    )
    side: Mapped[str] = mapped_column(Text, nullable=False)
    order_price: Mapped[float] = mapped_column(Numeric(20, 4), nullable=False)
    quantity: Mapped[float] = mapped_column(Numeric(20, 8), nullable=False)
    current_price: Mapped[float | None] = mapped_column(Numeric(20, 4))
    gap_pct: Mapped[float | None] = mapped_column(Numeric(8, 4))
    days_pending: Mapped[int | None] = mapped_column(Integer)
    account: Mapped[str] = mapped_column(Text, nullable=False)
    order_id: Mapped[str | None] = mapped_column(Text)
    resolved_as: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    resolved_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
```

**Step 2: Verify model imports**

Run: `uv run python -c "from app.models.review import Trade, TradeSnapshot, TradeReview, PendingSnapshot; print('OK')"` 
Expected: `OK`

**Step 3: Commit**

```bash
git add app/models/review.py
git commit -m "feat: add review schema SQLAlchemy models for trade review system"
```

---

### Task 2: Update model exports

**Files:**
- Modify: `app/models/__init__.py`

**Step 1: Add review imports to `app/models/__init__.py`**

After the existing `from .trading import ...` line, add:

```python
from .review import PendingSnapshot, Trade, TradeReview, TradeSnapshot
```

Add to `__all__` list (before the commented-out entries):

```python
    "Trade",
    "TradeSnapshot",
    "TradeReview",
    "PendingSnapshot",
```

**Step 2: Verify exports**

Run: `uv run python -c "from app.models import Trade, TradeSnapshot, TradeReview, PendingSnapshot; print('OK')"`
Expected: `OK`

**Step 3: Commit**

```bash
git add app/models/__init__.py
git commit -m "feat: export review models from app.models"
```

---

### Task 3: Write Alembic migration

**Files:**
- Create: `alembic/versions/<revision>_remove_dca_add_review_schema.py`

**Step 1: Generate migration skeleton**

Run: `uv run alembic revision -m "remove_dca_add_review_schema"`
Expected: Creates file in `alembic/versions/`

**Step 2: Replace generated migration with full implementation**

The `down_revision` should be `"86961c84a0ce"` (current head).

Replace the entire file content with:

```python
"""Remove legacy DCA tables, add review schema and tables.

Revision ID: <auto-generated>
Revises: 86961c84a0ce
Create Date: <auto-generated>
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "<keep-auto-generated>"
down_revision: str | Sequence[str] | None = "86961c84a0ce"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Existing instrument_type enum — reuse, do not create
instrument_type_enum = sa.Enum(
    "equity_kr", "equity_us", "crypto", "forex", "index",
    name="instrument_type",
    create_type=False,
)


def upgrade() -> None:
    # ------------------------------------------------------------------
    # 1. Drop legacy DCA tables (0 rows, no model, no runtime references)
    # ------------------------------------------------------------------
    op.drop_index(
        op.f("ix_dca_plan_steps_order_id"), table_name="dca_plan_steps"
    )
    op.drop_index(
        op.f("ix_dca_plan_steps_plan_id"), table_name="dca_plan_steps"
    )
    op.drop_table("dca_plan_steps")

    op.drop_index(op.f("ix_dca_plans_symbol"), table_name="dca_plans")
    op.drop_index(op.f("ix_dca_plans_user_status"), table_name="dca_plans")
    op.drop_table("dca_plans")

    op.execute("DROP TYPE IF EXISTS dca_step_status")
    op.execute("DROP TYPE IF EXISTS dca_plan_status")

    # ------------------------------------------------------------------
    # 2. Create review schema
    # ------------------------------------------------------------------
    op.execute("CREATE SCHEMA IF NOT EXISTS review")

    # ------------------------------------------------------------------
    # 3. review.trades
    # ------------------------------------------------------------------
    op.create_table(
        "trades",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("trade_date", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("instrument_type", instrument_type_enum, nullable=False),
        sa.Column("side", sa.Text(), nullable=False),
        sa.Column("price", sa.Numeric(20, 4), nullable=False),
        sa.Column("quantity", sa.Numeric(20, 8), nullable=False),
        sa.Column("total_amount", sa.Numeric(20, 4), nullable=False),
        sa.Column(
            "fee", sa.Numeric(20, 4), nullable=False, server_default="0"
        ),
        sa.Column(
            "currency", sa.Text(), nullable=False, server_default="KRW"
        ),
        sa.Column("account", sa.Text(), nullable=False),
        sa.Column("order_id", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "account", "order_id", name="uq_review_trades_account_order"
        ),
        sa.CheckConstraint(
            "side IN ('buy','sell')", name="review_trades_side"
        ),
        sa.CheckConstraint(
            "currency IN ('KRW','USD')", name="review_trades_currency"
        ),
        schema="review",
    )
    op.create_index(
        "ix_review_trades_trade_date",
        "trades",
        ["trade_date"],
        schema="review",
    )
    op.create_index(
        "ix_review_trades_symbol", "trades", ["symbol"], schema="review"
    )

    # ------------------------------------------------------------------
    # 4. review.trade_snapshots
    # ------------------------------------------------------------------
    op.create_table(
        "trade_snapshots",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("trade_id", sa.BigInteger(), nullable=False),
        sa.Column("rsi_14", sa.Numeric(6, 2), nullable=True),
        sa.Column("rsi_7", sa.Numeric(6, 2), nullable=True),
        sa.Column("ema_20", sa.Numeric(20, 4), nullable=True),
        sa.Column("ema_200", sa.Numeric(20, 4), nullable=True),
        sa.Column("macd", sa.Numeric(20, 4), nullable=True),
        sa.Column("macd_signal", sa.Numeric(20, 4), nullable=True),
        sa.Column("adx", sa.Numeric(6, 2), nullable=True),
        sa.Column("stoch_rsi_k", sa.Numeric(6, 2), nullable=True),
        sa.Column("volume_ratio", sa.Numeric(10, 2), nullable=True),
        sa.Column("fear_greed", sa.SmallInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["trade_id"],
            ["review.trades.id"],
            ondelete="CASCADE",
            name="fk_review_trade_snapshots_trade_id",
        ),
        sa.UniqueConstraint(
            "trade_id", name="uq_review_trade_snapshots_trade_id"
        ),
        schema="review",
    )

    # ------------------------------------------------------------------
    # 5. review.trade_reviews
    # ------------------------------------------------------------------
    op.create_table(
        "trade_reviews",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("trade_id", sa.BigInteger(), nullable=False),
        sa.Column("review_date", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("price_at_review", sa.Numeric(20, 4), nullable=True),
        sa.Column("pnl_pct", sa.Numeric(8, 4), nullable=True),
        sa.Column("verdict", sa.Text(), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column(
            "review_type",
            sa.Text(),
            nullable=False,
            server_default="daily",
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["trade_id"],
            ["review.trades.id"],
            ondelete="CASCADE",
            name="fk_review_trade_reviews_trade_id",
        ),
        sa.CheckConstraint(
            "verdict IN ('good','neutral','bad')",
            name="review_trade_reviews_verdict",
        ),
        sa.CheckConstraint(
            "review_type IN ('daily','weekly','monthly','manual')",
            name="review_trade_reviews_review_type",
        ),
        schema="review",
    )
    op.create_index(
        "ix_review_trade_reviews_trade_type",
        "trade_reviews",
        ["trade_id", "review_type"],
        schema="review",
    )
    op.create_index(
        "ix_review_trade_reviews_review_date",
        "trade_reviews",
        ["review_date"],
        schema="review",
    )

    # ------------------------------------------------------------------
    # 6. review.pending_snapshots
    # ------------------------------------------------------------------
    op.create_table(
        "pending_snapshots",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "snapshot_date", sa.TIMESTAMP(timezone=True), nullable=False
        ),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("instrument_type", instrument_type_enum, nullable=False),
        sa.Column("side", sa.Text(), nullable=False),
        sa.Column("order_price", sa.Numeric(20, 4), nullable=False),
        sa.Column("quantity", sa.Numeric(20, 8), nullable=False),
        sa.Column("current_price", sa.Numeric(20, 4), nullable=True),
        sa.Column("gap_pct", sa.Numeric(8, 4), nullable=True),
        sa.Column("days_pending", sa.Integer(), nullable=True),
        sa.Column("account", sa.Text(), nullable=False),
        sa.Column("order_id", sa.Text(), nullable=True),
        sa.Column(
            "resolved_as",
            sa.Text(),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("resolved_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "side IN ('buy','sell')", name="review_pending_side"
        ),
        sa.CheckConstraint(
            "resolved_as IN ('pending','filled','cancelled','expired')",
            name="review_pending_resolved_as",
        ),
        schema="review",
    )
    op.create_index(
        "ix_review_pending_resolved_date",
        "pending_snapshots",
        ["resolved_as", "snapshot_date"],
        schema="review",
    )
    op.create_index(
        "ix_review_pending_account_order_date",
        "pending_snapshots",
        ["account", "order_id", "snapshot_date"],
        schema="review",
    )


def downgrade() -> None:
    # Drop review tables (reverse order of creation)
    op.drop_index(
        "ix_review_pending_account_order_date",
        table_name="pending_snapshots",
        schema="review",
    )
    op.drop_index(
        "ix_review_pending_resolved_date",
        table_name="pending_snapshots",
        schema="review",
    )
    op.drop_table("pending_snapshots", schema="review")

    op.drop_index(
        "ix_review_trade_reviews_review_date",
        table_name="trade_reviews",
        schema="review",
    )
    op.drop_index(
        "ix_review_trade_reviews_trade_type",
        table_name="trade_reviews",
        schema="review",
    )
    op.drop_table("trade_reviews", schema="review")

    op.drop_table("trade_snapshots", schema="review")

    op.drop_index(
        "ix_review_trades_symbol", table_name="trades", schema="review"
    )
    op.drop_index(
        "ix_review_trades_trade_date", table_name="trades", schema="review"
    )
    op.drop_table("trades", schema="review")

    op.execute("DROP SCHEMA IF EXISTS review")

    # NOTE: DCA tables are NOT restored on downgrade — they were unused.
```

**Step 3: Verify migration file is valid**

Run: `uv run alembic check`
Expected: No errors (or only pre-existing warnings)

**Step 4: Commit**

```bash
git add alembic/versions/*remove_dca_add_review_schema*
git commit -m "feat: migration to drop DCA tables and create review schema"
```

---

### Task 4: Run migration and verify

**Step 1: Run migration against local DB**

Run: `uv run alembic upgrade head`
Expected: Migration executes without errors

**Step 2: Verify review schema exists**

Run: `uv run python -c "
import asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from app.core.config import settings

async def check():
    engine = create_async_engine(settings.DATABASE_URL)
    async with engine.connect() as conn:
        # Check review schema
        r = await conn.execute(text(\"SELECT schema_name FROM information_schema.schemata WHERE schema_name = 'review'\"))
        print(f'review schema: {r.fetchone()}')
        # Check review tables
        r = await conn.execute(text(\"SELECT table_name FROM information_schema.tables WHERE table_schema = 'review' ORDER BY table_name\"))
        tables = [row[0] for row in r.fetchall()]
        print(f'review tables: {tables}')
        # Check DCA tables gone
        r = await conn.execute(text(\"SELECT table_name FROM information_schema.tables WHERE table_name IN ('dca_plans','dca_plan_steps') AND table_schema = 'public'\"))
        dca = [row[0] for row in r.fetchall()]
        print(f'dca tables (should be empty): {dca}')
    await engine.dispose()

asyncio.run(check())
"`

Expected:
```
review schema: ('review',)
review tables: ['pending_snapshots', 'trade_reviews', 'trade_snapshots', 'trades']
dca tables (should be empty): []
```

**Step 3: Verify model import from top-level package**

Run: `uv run python -c "from app.models import Trade, TradeSnapshot, TradeReview, PendingSnapshot; print('All review models imported OK')"`
Expected: `All review models imported OK`

**Step 4: Run existing tests to confirm nothing broke**

Run: `uv run pytest tests/ -m "not live" -x -q`
Expected: All existing tests pass

**Step 5: Commit (if any verification-driven fixes were needed)**

```bash
git add -A
git commit -m "fix: address any migration verification issues"
```

---

### Task 5: Final lint and type check

**Step 1: Run linter**

Run: `uv run ruff check app/models/review.py`
Expected: No errors

**Step 2: Run formatter**

Run: `uv run ruff format --check app/models/review.py`
Expected: No changes needed (or apply `ruff format app/models/review.py`)

**Step 3: Run type checker**

Run: `uv run ty check app/models/review.py`
Expected: No errors (or only pre-existing warnings)

**Step 4: Final commit if formatting changed anything**

```bash
git add app/models/review.py
git commit -m "style: format review models"
```

---

## Design Decisions Log

| Decision | Rationale |
|----------|-----------|
| No abstract `ReviewBase` class | Follows `research_backtest.py` pattern — each model specifies `{"schema": "review"}` directly |
| `Text` instead of `VARCHAR(N)` | Matches project convention in `trading.py`, `trade_profile.py` for non-length-critical columns |
| `create_type=False` for `instrument_type` | Enum already exists in DB from init migration — reuse, don't recreate |
| `func.now()` in models, `sa.text("now()")` in migration | Models use `sqlalchemy.sql.func.now()` (matching `trade_profile.py`), migration uses raw SQL default (matching `f2c1e9b7a4d0`) |
| DCA tables not restored on downgrade | Tables were empty and unused — no data loss concern |
| `user_trade_defaults` NOT dropped | Active model + service + API + tests found despite spec claiming otherwise |
| CheckConstraint names use `review_` prefix (not `ck_review_`) | The `ck_` prefix comes from metadata naming convention auto-prepend. Explicit names in `__table_args__` should NOT include `ck_` since `Base.metadata` naming convention adds `ck_{table}_{name}` automatically. However, since we use explicit `name=` in CheckConstraint, the naming convention is bypassed. Using `review_` prefix for clarity. |
| Indexes named `ix_review_*` | Explicit index names bypass metadata naming convention. Using `ix_review_` prefix for cross-schema clarity. |
