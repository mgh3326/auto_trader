# Trade Journal MCP + Order Fills Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add permanent order fill recording and a trade journal system so AI can remember *why* it bought a position and respect hold periods before recommending sells.

**Architecture:** Two phases. Phase 1 hooks `_place_order_impl()` to auto-save executed orders into `review.trades`. Phase 2 adds a `review.trade_journals` table for investment thesis/strategy metadata, plus three MCP tools (`save_trade_journal`, `get_trade_journal`, `update_trade_journal`). The two phases are connected: successful `place_order` auto-transitions matching draft journals to active.

**Tech Stack:** Python 3.13, SQLAlchemy 2.0 async, Alembic, FastMCP, PostgreSQL (review schema), pytest + pytest-asyncio

---

## File Structure

### New Files
| File | Responsibility |
|------|---------------|
| `app/models/trade_journal.py` | `TradeJournal` ORM model in `review` schema |
| `app/mcp_server/tooling/trade_journal_tools.py` | Tool implementations: `save_trade_journal`, `get_trade_journal`, `update_trade_journal` |
| `app/mcp_server/tooling/trade_journal_registration.py` | MCP registration wrapper |
| `alembic/versions/xxxx_add_trade_journals_table.py` | Migration for `review.trade_journals` |
| `tests/test_trade_journal_model.py` | Model unit tests |
| `tests/test_mcp_trade_journal.py` | MCP tool integration tests |

### Modified Files
| File | Change |
|------|--------|
| `app/models/__init__.py` | Add `TradeJournal`, `JournalStatus` imports |
| `app/mcp_server/tooling/order_execution.py` | Add `_save_order_fill()` and `_link_journal_to_fill()` after successful execution |
| `app/mcp_server/tooling/registry.py` | Add `register_trade_journal_tools(mcp)` call |
| `app/mcp_server/tooling/__init__.py` | Add exports for journal registration |
| `tests/_mcp_tooling_support.py` | Add `trade_journal_tools` to `_PATCH_MODULES` |

---

## Task 1: TradeJournal Model

**Files:**
- Create: `app/models/trade_journal.py`
- Test: `tests/test_trade_journal_model.py`

- [ ] **Step 1: Write model unit test**

```python
# tests/test_trade_journal_model.py
"""Unit tests for TradeJournal model."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.models.trade_journal import JournalStatus, TradeJournal
from app.models.trading import InstrumentType


class TestTradeJournalModel:
    def test_create_minimal_journal(self) -> None:
        journal = TradeJournal(
            symbol="KRW-BTC",
            instrument_type=InstrumentType.crypto,
            thesis="RSI oversold bounce play",
        )
        assert journal.symbol == "KRW-BTC"
        assert journal.instrument_type == InstrumentType.crypto
        assert journal.thesis == "RSI oversold bounce play"
        assert journal.side == "buy"
        assert journal.status == "draft"

    def test_create_full_journal(self) -> None:
        now = datetime.now(timezone.utc)
        journal = TradeJournal(
            symbol="AAPL",
            instrument_type=InstrumentType.equity_us,
            side="buy",
            thesis="Strong earnings momentum",
            strategy="momentum",
            entry_price=Decimal("175.50"),
            quantity=Decimal("10"),
            amount=Decimal("1755.00"),
            target_price=Decimal("200.00"),
            stop_loss=Decimal("160.00"),
            min_hold_days=14,
            hold_until=now + timedelta(days=14),
            indicators_snapshot={"rsi_14": 42, "adx": 25},
            status="active",
            account="kis",
        )
        assert journal.strategy == "momentum"
        assert journal.target_price == Decimal("200.00")
        assert journal.stop_loss == Decimal("160.00")
        assert journal.min_hold_days == 14
        assert journal.indicators_snapshot == {"rsi_14": 42, "adx": 25}

    def test_journal_status_enum_values(self) -> None:
        assert JournalStatus.draft == "draft"
        assert JournalStatus.active == "active"
        assert JournalStatus.closed == "closed"
        assert JournalStatus.stopped == "stopped"
        assert JournalStatus.expired == "expired"

    def test_pnl_calculation(self) -> None:
        journal = TradeJournal(
            symbol="KRW-BTC",
            instrument_type=InstrumentType.crypto,
            thesis="test",
            entry_price=Decimal("100.00"),
            exit_price=Decimal("120.00"),
        )
        expected_pnl = (Decimal("120.00") / Decimal("100.00") - 1) * 100
        assert float(expected_pnl) == pytest.approx(20.0)

    def test_table_args(self) -> None:
        assert TradeJournal.__table_args__[-1] == {"schema": "review"}
        assert TradeJournal.__tablename__ == "trade_journals"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_trade_journal_model.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.models.trade_journal'`

- [ ] **Step 3: Write the TradeJournal model**

```python
# app/models/trade_journal.py
"""Trade journal — investment thesis and strategy metadata."""
from __future__ import annotations

import enum
from datetime import datetime
from decimal import Decimal

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
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.trading import InstrumentType


class JournalStatus(enum.StrEnum):
    draft = "draft"
    active = "active"
    closed = "closed"
    stopped = "stopped"
    expired = "expired"


class TradeJournal(Base):
    __tablename__ = "trade_journals"
    __table_args__ = (
        CheckConstraint(
            "status IN ('draft','active','closed','stopped','expired')",
            name="trade_journals_status_allowed",
        ),
        CheckConstraint(
            "side IN ('buy','sell')",
            name="trade_journals_side",
        ),
        Index("ix_trade_journals_symbol_status", "symbol", "status"),
        Index("ix_trade_journals_created", "created_at"),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    # Symbol info
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    instrument_type: Mapped[InstrumentType] = mapped_column(
        Enum(InstrumentType, name="instrument_type", create_type=False),
        nullable=False,
    )
    side: Mapped[str] = mapped_column(Text, nullable=False, default="buy")

    # Price/quantity at recommendation time
    entry_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))
    quantity: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    amount: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))

    # Strategy metadata (the core value!)
    thesis: Mapped[str] = mapped_column(Text, nullable=False)
    strategy: Mapped[str | None] = mapped_column(Text)
    target_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))
    stop_loss: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))
    min_hold_days: Mapped[int | None] = mapped_column(SmallInteger)
    hold_until: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))

    # Indicator snapshot at entry
    indicators_snapshot: Mapped[dict | None] = mapped_column(JSONB)

    # Status
    status: Mapped[str] = mapped_column(Text, nullable=False, default="draft")

    # Link to review.trades (optional)
    trade_id: Mapped[int | None] = mapped_column(
        ForeignKey("review.trades.id", ondelete="SET NULL"),
    )

    # Exit info
    exit_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))
    exit_date: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    exit_reason: Mapped[str | None] = mapped_column(Text)
    pnl_pct: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))

    # Meta
    account: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_trade_journal_model.py -v`
Expected: All 5 tests PASS

- [ ] **Step 5: Register model in `app/models/__init__.py`**

Add these imports to `app/models/__init__.py`:

```python
# After the existing review imports line:
from .trade_journal import JournalStatus, TradeJournal
```

Add to `__all__`:
```python
"TradeJournal",
"JournalStatus",
```

- [ ] **Step 6: Commit**

```bash
git add app/models/trade_journal.py app/models/__init__.py tests/test_trade_journal_model.py
git commit -m "feat: add TradeJournal model for investment thesis storage"
```

---

## Task 2: Alembic Migration

**Files:**
- Create: `alembic/versions/xxxx_add_trade_journals_table.py`

- [ ] **Step 1: Generate the migration**

Run: `uv run alembic revision --autogenerate -m "add trade_journals table"`

- [ ] **Step 2: Verify the generated migration**

Open the generated file in `alembic/versions/` and verify it contains:
- `op.create_table("trade_journals", ..., schema="review")`
- All columns matching the model
- The `instrument_type` enum reuse with `create_type=False`
- Both check constraints and both indexes
- FK to `review.trades.id`

The migration should look like this (verify and fix if autogenerate missed anything):

```python
"""add trade_journals table

Revision ID: <auto>
Revises: <auto>
Create Date: <auto>
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "<auto>"
down_revision: str | Sequence[str] | None = "<auto>"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

instrument_type_enum = sa.Enum(
    "equity_kr", "equity_us", "crypto", "forex", "index",
    name="instrument_type",
    create_type=False,
)


def upgrade() -> None:
    op.create_table(
        "trade_journals",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("instrument_type", instrument_type_enum, nullable=False),
        sa.Column("side", sa.Text(), nullable=False, server_default="buy"),
        sa.Column("entry_price", sa.Numeric(20, 4), nullable=True),
        sa.Column("quantity", sa.Numeric(20, 8), nullable=True),
        sa.Column("amount", sa.Numeric(20, 4), nullable=True),
        sa.Column("thesis", sa.Text(), nullable=False),
        sa.Column("strategy", sa.Text(), nullable=True),
        sa.Column("target_price", sa.Numeric(20, 4), nullable=True),
        sa.Column("stop_loss", sa.Numeric(20, 4), nullable=True),
        sa.Column("min_hold_days", sa.SmallInteger(), nullable=True),
        sa.Column("hold_until", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("indicators_snapshot", postgresql.JSONB(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="draft"),
        sa.Column("trade_id", sa.BigInteger(), nullable=True),
        sa.Column("exit_price", sa.Numeric(20, 4), nullable=True),
        sa.Column("exit_date", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("exit_reason", sa.Text(), nullable=True),
        sa.Column("pnl_pct", sa.Numeric(8, 4), nullable=True),
        sa.Column("account", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["trade_id"],
            ["review.trades.id"],
            ondelete="SET NULL",
            name="fk_trade_journals_trade_id_trades",
        ),
        sa.CheckConstraint(
            "status IN ('draft','active','closed','stopped','expired')",
            name="trade_journals_status_allowed",
        ),
        sa.CheckConstraint(
            "side IN ('buy','sell')",
            name="trade_journals_side",
        ),
        schema="review",
    )
    op.create_index(
        "ix_trade_journals_symbol_status",
        "trade_journals",
        ["symbol", "status"],
        schema="review",
    )
    op.create_index(
        "ix_trade_journals_created",
        "trade_journals",
        ["created_at"],
        schema="review",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_trade_journals_created",
        table_name="trade_journals",
        schema="review",
    )
    op.drop_index(
        "ix_trade_journals_symbol_status",
        table_name="trade_journals",
        schema="review",
    )
    op.drop_table("trade_journals", schema="review")
```

- [ ] **Step 3: Apply the migration**

Run: `uv run alembic upgrade head`
Expected: `Running upgrade ... -> ..., add trade_journals table`

- [ ] **Step 4: Verify table exists**

Run: `uv run alembic current`
Expected: Shows the new revision as current head

- [ ] **Step 5: Commit**

```bash
git add alembic/versions/*add_trade_journals*
git commit -m "migrate: add review.trade_journals table"
```

---

## Task 3: Trade Journal MCP Tools — `save_trade_journal`

**Files:**
- Create: `app/mcp_server/tooling/trade_journal_tools.py`
- Test: `tests/test_mcp_trade_journal.py`

- [ ] **Step 1: Write the failing test for save_trade_journal**

```python
# tests/test_mcp_trade_journal.py
"""MCP tool tests for trade journal."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.trade_journal import JournalStatus, TradeJournal
from app.models.trading import InstrumentType


def _build_session_cm(session: AsyncMock) -> AsyncMock:
    """Build an async context manager that yields the mock session."""
    session_cm = AsyncMock()
    session_cm.__aenter__.return_value = session
    session_cm.__aexit__.return_value = None
    return session_cm


def _mock_session_factory(session: AsyncMock) -> MagicMock:
    return MagicMock(return_value=_build_session_cm(session))


class TestSaveTradeJournal:
    @pytest.mark.asyncio
    async def test_save_draft_journal_crypto(self) -> None:
        from app.mcp_server.tooling.trade_journal_tools import save_trade_journal

        mock_session = AsyncMock()
        # Mock: no existing active journal for this symbol
        mock_scalars = MagicMock()
        mock_scalars.first.return_value = None
        mock_result = MagicMock()
        mock_result.scalars.return_value = mock_scalars
        mock_session.execute.return_value = mock_result

        factory = _mock_session_factory(mock_session)
        with patch(
            "app.mcp_server.tooling.trade_journal_tools._session_factory",
            return_value=factory,
        ):
            result = await save_trade_journal(
                symbol="KRW-BTC",
                thesis="RSI oversold at 28, ADX rising — bounce expected",
                entry_price=95000000.0,
                target_price=105000000.0,
                stop_loss=90000000.0,
                min_hold_days=7,
                strategy="dca_oversold",
                indicators_snapshot={"rsi_14": 28, "adx": 22},
            )

        assert result["success"] is True
        assert result["action"] == "created"
        # Verify session.add was called with a TradeJournal
        added_obj = mock_session.add.call_args[0][0]
        assert isinstance(added_obj, TradeJournal)
        assert added_obj.symbol == "KRW-BTC"
        assert added_obj.instrument_type == InstrumentType.crypto
        assert added_obj.thesis == "RSI oversold at 28, ADX rising — bounce expected"
        assert added_obj.status == "draft"
        assert added_obj.min_hold_days == 7
        assert added_obj.hold_until is not None

    @pytest.mark.asyncio
    async def test_save_warns_on_existing_active(self) -> None:
        from app.mcp_server.tooling.trade_journal_tools import save_trade_journal

        mock_session = AsyncMock()
        # Mock: existing active journal found
        existing = TradeJournal(
            id=1,
            symbol="KRW-BTC",
            instrument_type=InstrumentType.crypto,
            thesis="old thesis",
            status="active",
        )
        mock_scalars = MagicMock()
        mock_scalars.first.return_value = existing
        mock_result = MagicMock()
        mock_result.scalars.return_value = mock_scalars
        mock_session.execute.return_value = mock_result

        factory = _mock_session_factory(mock_session)
        with patch(
            "app.mcp_server.tooling.trade_journal_tools._session_factory",
            return_value=factory,
        ):
            result = await save_trade_journal(
                symbol="KRW-BTC",
                thesis="New thesis",
            )

        assert result["success"] is True
        assert "warning" in result

    @pytest.mark.asyncio
    async def test_save_rejects_empty_thesis(self) -> None:
        from app.mcp_server.tooling.trade_journal_tools import save_trade_journal

        result = await save_trade_journal(symbol="KRW-BTC", thesis="")
        assert result["success"] is False
        assert "thesis" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_save_detects_instrument_type_us(self) -> None:
        from app.mcp_server.tooling.trade_journal_tools import save_trade_journal

        mock_session = AsyncMock()
        mock_scalars = MagicMock()
        mock_scalars.first.return_value = None
        mock_result = MagicMock()
        mock_result.scalars.return_value = mock_scalars
        mock_session.execute.return_value = mock_result

        factory = _mock_session_factory(mock_session)
        with patch(
            "app.mcp_server.tooling.trade_journal_tools._session_factory",
            return_value=factory,
        ):
            result = await save_trade_journal(
                symbol="AAPL",
                thesis="Strong Q1 earnings beat",
            )

        assert result["success"] is True
        added_obj = mock_session.add.call_args[0][0]
        assert added_obj.instrument_type == InstrumentType.equity_us
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_mcp_trade_journal.py::TestSaveTradeJournal -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.mcp_server.tooling.trade_journal_tools'`

- [ ] **Step 3: Implement save_trade_journal**

```python
# app/mcp_server/tooling/trade_journal_tools.py
"""Trade journal MCP tool implementations."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, cast

from sqlalchemy import select, and_, desc
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db import AsyncSessionLocal
from app.core.timezone import now_kst
from app.mcp_server.tooling.shared import (
    resolve_market_type as _resolve_market_type,
)
from app.models.trade_journal import JournalStatus, TradeJournal
from app.models.trading import InstrumentType

logger = logging.getLogger(__name__)


def _session_factory() -> async_sessionmaker[AsyncSession]:
    return cast(async_sessionmaker[AsyncSession], cast(object, AsyncSessionLocal))


def _serialize_journal(j: TradeJournal) -> dict[str, Any]:
    """Convert a TradeJournal row to a JSON-safe dict."""
    return {
        "id": j.id,
        "symbol": j.symbol,
        "instrument_type": j.instrument_type.value if hasattr(j.instrument_type, "value") else str(j.instrument_type),
        "side": j.side,
        "entry_price": float(j.entry_price) if j.entry_price is not None else None,
        "quantity": float(j.quantity) if j.quantity is not None else None,
        "amount": float(j.amount) if j.amount is not None else None,
        "thesis": j.thesis,
        "strategy": j.strategy,
        "target_price": float(j.target_price) if j.target_price is not None else None,
        "stop_loss": float(j.stop_loss) if j.stop_loss is not None else None,
        "min_hold_days": j.min_hold_days,
        "hold_until": j.hold_until.isoformat() if j.hold_until else None,
        "indicators_snapshot": j.indicators_snapshot,
        "status": j.status,
        "trade_id": j.trade_id,
        "exit_price": float(j.exit_price) if j.exit_price is not None else None,
        "exit_date": j.exit_date.isoformat() if j.exit_date else None,
        "exit_reason": j.exit_reason,
        "pnl_pct": float(j.pnl_pct) if j.pnl_pct is not None else None,
        "account": j.account,
        "notes": j.notes,
        "created_at": j.created_at.isoformat() if j.created_at else None,
        "updated_at": j.updated_at.isoformat() if j.updated_at else None,
    }


async def save_trade_journal(
    symbol: str,
    thesis: str,
    side: str = "buy",
    entry_price: float | None = None,
    quantity: float | None = None,
    amount: float | None = None,
    strategy: str | None = None,
    target_price: float | None = None,
    stop_loss: float | None = None,
    min_hold_days: int | None = None,
    indicators_snapshot: dict | None = None,
    account: str | None = None,
    notes: str | None = None,
    status: str = "draft",
) -> dict[str, Any]:
    """Save a trade journal entry with investment thesis and strategy metadata.

    symbol is auto-detected for instrument_type (KRW-BTC -> crypto, AAPL -> equity_us, 005930 -> equity_kr).
    min_hold_days auto-calculates hold_until from now.
    Warns if an active journal already exists for the same symbol.
    """
    symbol = (symbol or "").strip()
    thesis = (thesis or "").strip()

    if not symbol:
        return {"success": False, "error": "symbol is required"}
    if not thesis:
        return {"success": False, "error": "thesis is required"}
    if side not in ("buy", "sell"):
        return {"success": False, "error": "side must be 'buy' or 'sell'"}
    if status not in {s.value for s in JournalStatus}:
        return {"success": False, "error": f"Invalid status: {status}"}

    try:
        market_type, normalized_symbol = _resolve_market_type(symbol, None)
    except ValueError as exc:
        return {"success": False, "error": f"Cannot detect market type: {exc}"}

    instrument = InstrumentType(market_type)

    hold_until = None
    if min_hold_days is not None and min_hold_days > 0:
        hold_until = now_kst() + timedelta(days=min_hold_days)

    try:
        async with _session_factory()() as db:
            # Check for existing active journal
            warning = None
            existing_stmt = (
                select(TradeJournal)
                .where(
                    TradeJournal.symbol == normalized_symbol,
                    TradeJournal.status == JournalStatus.active,
                )
                .order_by(desc(TradeJournal.created_at))
                .limit(1)
            )
            existing_result = await db.execute(existing_stmt)
            existing = existing_result.scalars().first()
            if existing:
                warning = (
                    f"Active journal already exists for {normalized_symbol} "
                    f"(id={existing.id}, thesis='{existing.thesis[:50]}...'). "
                    "Creating new journal anyway."
                )

            journal = TradeJournal(
                symbol=normalized_symbol,
                instrument_type=instrument,
                side=side,
                entry_price=Decimal(str(entry_price)) if entry_price is not None else None,
                quantity=Decimal(str(quantity)) if quantity is not None else None,
                amount=Decimal(str(amount)) if amount is not None else None,
                thesis=thesis,
                strategy=strategy,
                target_price=Decimal(str(target_price)) if target_price is not None else None,
                stop_loss=Decimal(str(stop_loss)) if stop_loss is not None else None,
                min_hold_days=min_hold_days,
                hold_until=hold_until,
                indicators_snapshot=indicators_snapshot,
                status=status,
                account=account,
                notes=notes,
            )
            db.add(journal)
            await db.commit()
            await db.refresh(journal)

            result: dict[str, Any] = {
                "success": True,
                "action": "created",
                "data": _serialize_journal(journal),
            }
            if warning:
                result["warning"] = warning
            return result

    except Exception as exc:
        logger.exception("save_trade_journal failed")
        return {"success": False, "error": f"save_trade_journal failed: {exc}"}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_mcp_trade_journal.py::TestSaveTradeJournal -v`
Expected: All 4 tests PASS

- [ ] **Step 5: Commit**

```bash
git add app/mcp_server/tooling/trade_journal_tools.py tests/test_mcp_trade_journal.py
git commit -m "feat: add save_trade_journal MCP tool"
```

---

## Task 4: Trade Journal MCP Tools — `get_trade_journal`

**Files:**
- Modify: `app/mcp_server/tooling/trade_journal_tools.py`
- Modify: `tests/test_mcp_trade_journal.py`

- [ ] **Step 1: Write the failing tests for get_trade_journal**

Append to `tests/test_mcp_trade_journal.py`:

```python
class TestGetTradeJournal:
    @pytest.mark.asyncio
    async def test_get_active_journals(self) -> None:
        from app.mcp_server.tooling.trade_journal_tools import get_trade_journal

        now = datetime.now(timezone.utc)
        journals = [
            TradeJournal(
                id=1,
                symbol="KRW-BTC",
                instrument_type=InstrumentType.crypto,
                thesis="RSI oversold",
                status="active",
                entry_price=Decimal("95000000"),
                target_price=Decimal("105000000"),
                stop_loss=Decimal("90000000"),
                hold_until=now + timedelta(days=5),
                min_hold_days=7,
                created_at=now,
                updated_at=now,
                side="buy",
            ),
        ]

        mock_session = AsyncMock()
        mock_scalars = MagicMock()
        mock_scalars.all.return_value = journals
        mock_result = MagicMock()
        mock_result.scalars.return_value = mock_scalars
        mock_session.execute.return_value = mock_result

        factory = _mock_session_factory(mock_session)
        with patch(
            "app.mcp_server.tooling.trade_journal_tools._session_factory",
            return_value=factory,
        ):
            result = await get_trade_journal(status="active")

        assert result["success"] is True
        assert result["summary"]["total_active"] == 1
        assert len(result["entries"]) == 1
        entry = result["entries"][0]
        assert entry["symbol"] == "KRW-BTC"
        assert entry["hold_expired"] is False
        assert entry["hold_remaining_days"] >= 0

    @pytest.mark.asyncio
    async def test_get_journal_by_symbol(self) -> None:
        from app.mcp_server.tooling.trade_journal_tools import get_trade_journal

        now = datetime.now(timezone.utc)
        journals = [
            TradeJournal(
                id=2,
                symbol="AAPL",
                instrument_type=InstrumentType.equity_us,
                thesis="Earnings play",
                status="active",
                hold_until=now - timedelta(days=1),
                min_hold_days=7,
                created_at=now - timedelta(days=8),
                updated_at=now,
                side="buy",
            ),
        ]

        mock_session = AsyncMock()
        mock_scalars = MagicMock()
        mock_scalars.all.return_value = journals
        mock_result = MagicMock()
        mock_result.scalars.return_value = mock_scalars
        mock_session.execute.return_value = mock_result

        factory = _mock_session_factory(mock_session)
        with patch(
            "app.mcp_server.tooling.trade_journal_tools._session_factory",
            return_value=factory,
        ):
            result = await get_trade_journal(symbol="AAPL")

        assert result["success"] is True
        entry = result["entries"][0]
        assert entry["hold_expired"] is True
        assert entry["hold_remaining_days"] < 0

    @pytest.mark.asyncio
    async def test_get_returns_empty_when_none_found(self) -> None:
        from app.mcp_server.tooling.trade_journal_tools import get_trade_journal

        mock_session = AsyncMock()
        mock_scalars = MagicMock()
        mock_scalars.all.return_value = []
        mock_result = MagicMock()
        mock_result.scalars.return_value = mock_scalars
        mock_session.execute.return_value = mock_result

        factory = _mock_session_factory(mock_session)
        with patch(
            "app.mcp_server.tooling.trade_journal_tools._session_factory",
            return_value=factory,
        ):
            result = await get_trade_journal(symbol="NONEXIST")

        assert result["success"] is True
        assert result["entries"] == []
        assert result["summary"]["total_active"] == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_mcp_trade_journal.py::TestGetTradeJournal -v`
Expected: FAIL with `ImportError: cannot import name 'get_trade_journal'`

- [ ] **Step 3: Implement get_trade_journal**

Add to `app/mcp_server/tooling/trade_journal_tools.py`:

```python
async def get_trade_journal(
    symbol: str | None = None,
    status: str | None = None,
    market: str | None = None,
    strategy: str | None = None,
    days: int | None = None,
    include_closed: bool = False,
    limit: int = 50,
) -> dict[str, Any]:
    """Query trade journals. Call before any sell decision to check thesis and hold periods.

    Returns active journals by default. Set include_closed=True for closed/stopped.
    Each entry includes hold_remaining_days, hold_expired for hold period checks.
    """
    try:
        async with _session_factory()() as db:
            filters = []

            if symbol:
                symbol = symbol.strip()
                try:
                    _, normalized = _resolve_market_type(symbol, None)
                    filters.append(TradeJournal.symbol == normalized)
                except ValueError:
                    filters.append(TradeJournal.symbol == symbol)

            if status:
                if status not in {s.value for s in JournalStatus}:
                    return {"success": False, "error": f"Invalid status: {status}"}
                filters.append(TradeJournal.status == status)
            elif not include_closed:
                filters.append(
                    TradeJournal.status.in_([
                        JournalStatus.draft,
                        JournalStatus.active,
                    ])
                )

            if market:
                market_map = {
                    "crypto": InstrumentType.crypto,
                    "kr": InstrumentType.equity_kr,
                    "us": InstrumentType.equity_us,
                }
                itype = market_map.get(market)
                if itype:
                    filters.append(TradeJournal.instrument_type == itype)

            if strategy:
                filters.append(TradeJournal.strategy == strategy)

            if days is not None and days > 0:
                cutoff = now_kst() - timedelta(days=days)
                filters.append(TradeJournal.created_at >= cutoff)

            stmt = (
                select(TradeJournal)
                .where(*filters)
                .order_by(desc(TradeJournal.created_at))
                .limit(limit)
            )
            result = await db.execute(stmt)
            journals = result.scalars().all()

            now = now_kst()
            entries = []
            total_active = 0
            hold_locked = 0
            near_target = 0
            near_stop = 0

            for j in journals:
                entry = _serialize_journal(j)

                # Hold period calculations
                if j.hold_until:
                    remaining = (j.hold_until - now).days
                    entry["hold_remaining_days"] = remaining
                    entry["hold_expired"] = remaining < 0
                    if remaining >= 0 and j.status == JournalStatus.active:
                        hold_locked += 1
                else:
                    entry["hold_remaining_days"] = None
                    entry["hold_expired"] = None

                # Current price not fetched here (too slow for bulk queries)
                # Caller can use get_quote separately
                entry["current_price"] = None
                entry["pnl_pct_live"] = None
                entry["target_reached"] = None
                entry["stop_reached"] = None

                if j.status == JournalStatus.active:
                    total_active += 1

                entries.append(entry)

            return {
                "success": True,
                "entries": entries,
                "summary": {
                    "total_active": total_active,
                    "hold_locked": hold_locked,
                    "near_target": near_target,
                    "near_stop": near_stop,
                    "total_returned": len(entries),
                },
            }

    except Exception as exc:
        logger.exception("get_trade_journal failed")
        return {"success": False, "error": f"get_trade_journal failed: {exc}"}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_mcp_trade_journal.py::TestGetTradeJournal -v`
Expected: All 3 tests PASS

- [ ] **Step 5: Commit**

```bash
git add app/mcp_server/tooling/trade_journal_tools.py tests/test_mcp_trade_journal.py
git commit -m "feat: add get_trade_journal MCP tool"
```

---

## Task 5: Trade Journal MCP Tools — `update_trade_journal`

**Files:**
- Modify: `app/mcp_server/tooling/trade_journal_tools.py`
- Modify: `tests/test_mcp_trade_journal.py`

- [ ] **Step 1: Write the failing tests for update_trade_journal**

Append to `tests/test_mcp_trade_journal.py`:

```python
class TestUpdateTradeJournal:
    @pytest.mark.asyncio
    async def test_update_draft_to_active(self) -> None:
        from app.mcp_server.tooling.trade_journal_tools import update_trade_journal

        now = datetime.now(timezone.utc)
        journal = TradeJournal(
            id=1,
            symbol="KRW-BTC",
            instrument_type=InstrumentType.crypto,
            thesis="RSI oversold",
            status="draft",
            entry_price=Decimal("95000000"),
            min_hold_days=7,
            created_at=now,
            updated_at=now,
            side="buy",
        )

        mock_session = AsyncMock()
        mock_session.get.return_value = journal

        factory = _mock_session_factory(mock_session)
        with patch(
            "app.mcp_server.tooling.trade_journal_tools._session_factory",
            return_value=factory,
        ):
            result = await update_trade_journal(journal_id=1, status="active", trade_id=100)

        assert result["success"] is True
        assert journal.status == "active"
        assert journal.trade_id == 100
        assert journal.hold_until is not None  # recalculated from now

    @pytest.mark.asyncio
    async def test_update_close_with_pnl(self) -> None:
        from app.mcp_server.tooling.trade_journal_tools import update_trade_journal

        now = datetime.now(timezone.utc)
        journal = TradeJournal(
            id=2,
            symbol="AAPL",
            instrument_type=InstrumentType.equity_us,
            thesis="Earnings play",
            status="active",
            entry_price=Decimal("175.50"),
            created_at=now - timedelta(days=10),
            updated_at=now,
            side="buy",
        )

        mock_session = AsyncMock()
        mock_session.get.return_value = journal

        factory = _mock_session_factory(mock_session)
        with patch(
            "app.mcp_server.tooling.trade_journal_tools._session_factory",
            return_value=factory,
        ):
            result = await update_trade_journal(
                journal_id=2,
                status="closed",
                exit_price=200.0,
                exit_reason="target_reached",
            )

        assert result["success"] is True
        assert journal.status == "closed"
        assert journal.exit_price == Decimal("200.0")
        assert journal.exit_reason == "target_reached"
        assert journal.exit_date is not None
        # pnl_pct = (200 / 175.5 - 1) * 100 ≈ 13.96
        assert float(journal.pnl_pct) == pytest.approx(13.96, abs=0.1)

    @pytest.mark.asyncio
    async def test_update_by_symbol_finds_latest_active(self) -> None:
        from app.mcp_server.tooling.trade_journal_tools import update_trade_journal

        now = datetime.now(timezone.utc)
        journal = TradeJournal(
            id=5,
            symbol="KRW-BTC",
            instrument_type=InstrumentType.crypto,
            thesis="test",
            status="active",
            created_at=now,
            updated_at=now,
            side="buy",
        )

        mock_session = AsyncMock()
        mock_session.get.return_value = None  # journal_id not provided
        mock_scalars = MagicMock()
        mock_scalars.first.return_value = journal
        mock_result = MagicMock()
        mock_result.scalars.return_value = mock_scalars
        mock_session.execute.return_value = mock_result

        factory = _mock_session_factory(mock_session)
        with patch(
            "app.mcp_server.tooling.trade_journal_tools._session_factory",
            return_value=factory,
        ):
            result = await update_trade_journal(
                symbol="KRW-BTC",
                notes="Updating strategy notes",
            )

        assert result["success"] is True
        assert journal.notes == "Updating strategy notes"

    @pytest.mark.asyncio
    async def test_update_not_found(self) -> None:
        from app.mcp_server.tooling.trade_journal_tools import update_trade_journal

        mock_session = AsyncMock()
        mock_session.get.return_value = None
        mock_scalars = MagicMock()
        mock_scalars.first.return_value = None
        mock_result = MagicMock()
        mock_result.scalars.return_value = mock_scalars
        mock_session.execute.return_value = mock_result

        factory = _mock_session_factory(mock_session)
        with patch(
            "app.mcp_server.tooling.trade_journal_tools._session_factory",
            return_value=factory,
        ):
            result = await update_trade_journal(journal_id=999)

        assert result["success"] is False
        assert "not found" in result["error"].lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_mcp_trade_journal.py::TestUpdateTradeJournal -v`
Expected: FAIL with `ImportError: cannot import name 'update_trade_journal'`

- [ ] **Step 3: Implement update_trade_journal**

Add to `app/mcp_server/tooling/trade_journal_tools.py`:

```python
async def update_trade_journal(
    journal_id: int | None = None,
    symbol: str | None = None,
    status: str | None = None,
    exit_price: float | None = None,
    exit_reason: str | None = None,
    target_price: float | None = None,
    stop_loss: float | None = None,
    min_hold_days: int | None = None,
    trade_id: int | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    """Update a trade journal entry.

    Find by journal_id, or by symbol (most recent active).
    On close/stop: auto-calculates pnl_pct from entry_price and exit_price.
    On activate: recalculates hold_until from now if min_hold_days is set.
    """
    if journal_id is None and not symbol:
        return {"success": False, "error": "Either journal_id or symbol is required"}

    try:
        async with _session_factory()() as db:
            journal: TradeJournal | None = None

            if journal_id is not None:
                journal = await db.get(TradeJournal, journal_id)

            if journal is None and symbol:
                symbol = symbol.strip()
                try:
                    _, normalized = _resolve_market_type(symbol, None)
                except ValueError:
                    normalized = symbol

                stmt = (
                    select(TradeJournal)
                    .where(
                        TradeJournal.symbol == normalized,
                        TradeJournal.status.in_([
                            JournalStatus.draft,
                            JournalStatus.active,
                        ]),
                    )
                    .order_by(desc(TradeJournal.created_at))
                    .limit(1)
                )
                result = await db.execute(stmt)
                journal = result.scalars().first()

            if journal is None:
                target = f"id={journal_id}" if journal_id else f"symbol={symbol}"
                return {"success": False, "error": f"Journal not found: {target}"}

            # Apply updates
            if status is not None:
                if status not in {s.value for s in JournalStatus}:
                    return {"success": False, "error": f"Invalid status: {status}"}
                journal.status = status

                # On activation: recalculate hold_until from now
                if status == JournalStatus.active and journal.min_hold_days:
                    journal.hold_until = now_kst() + timedelta(days=journal.min_hold_days)

            if trade_id is not None:
                journal.trade_id = trade_id

            if target_price is not None:
                journal.target_price = Decimal(str(target_price))

            if stop_loss is not None:
                journal.stop_loss = Decimal(str(stop_loss))

            if min_hold_days is not None:
                journal.min_hold_days = min_hold_days
                journal.hold_until = now_kst() + timedelta(days=min_hold_days)

            if notes is not None:
                journal.notes = notes

            if exit_price is not None:
                journal.exit_price = Decimal(str(exit_price))
                journal.exit_date = now_kst()

                # Auto-calculate pnl_pct
                if journal.entry_price and journal.entry_price > 0:
                    pnl = (Decimal(str(exit_price)) / journal.entry_price - 1) * 100
                    journal.pnl_pct = round(pnl, 4)

            if exit_reason is not None:
                journal.exit_reason = exit_reason

            await db.commit()
            await db.refresh(journal)

            return {
                "success": True,
                "action": "updated",
                "data": _serialize_journal(journal),
            }

    except Exception as exc:
        logger.exception("update_trade_journal failed")
        return {"success": False, "error": f"update_trade_journal failed: {exc}"}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_mcp_trade_journal.py::TestUpdateTradeJournal -v`
Expected: All 4 tests PASS

- [ ] **Step 5: Commit**

```bash
git add app/mcp_server/tooling/trade_journal_tools.py tests/test_mcp_trade_journal.py
git commit -m "feat: add update_trade_journal MCP tool"
```

---

## Task 6: MCP Registration & Wiring

**Files:**
- Create: `app/mcp_server/tooling/trade_journal_registration.py`
- Modify: `app/mcp_server/tooling/registry.py`
- Modify: `app/mcp_server/tooling/__init__.py`
- Modify: `tests/_mcp_tooling_support.py`

- [ ] **Step 1: Write registration test**

Append to `tests/test_mcp_trade_journal.py`:

```python
class TestTradeJournalRegistration:
    def test_tools_are_registered(self) -> None:
        from tests._mcp_tooling_support import build_tools

        tools = build_tools()
        assert "save_trade_journal" in tools
        assert "get_trade_journal" in tools
        assert "update_trade_journal" in tools

    def test_tool_names_set(self) -> None:
        from app.mcp_server.tooling.trade_journal_registration import (
            TRADE_JOURNAL_TOOL_NAMES,
        )

        assert TRADE_JOURNAL_TOOL_NAMES == {
            "save_trade_journal",
            "get_trade_journal",
            "update_trade_journal",
        }
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_mcp_trade_journal.py::TestTradeJournalRegistration -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Create trade_journal_registration.py**

```python
# app/mcp_server/tooling/trade_journal_registration.py
"""MCP registration for trade journal tools."""
from __future__ import annotations

from typing import TYPE_CHECKING

from app.mcp_server.tooling.trade_journal_tools import (
    get_trade_journal,
    save_trade_journal,
    update_trade_journal,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP

TRADE_JOURNAL_TOOL_NAMES: set[str] = {
    "save_trade_journal",
    "get_trade_journal",
    "update_trade_journal",
}


def register_trade_journal_tools(mcp: FastMCP) -> None:
    _ = mcp.tool(
        name="save_trade_journal",
        description=(
            "Save a trade journal entry with investment thesis and strategy metadata. "
            "Call this when recommending a buy/sell to record WHY. "
            "symbol auto-detects instrument_type. min_hold_days sets hold_until. "
            "status defaults to 'draft' — set to 'active' after fill confirmation."
        ),
    )(save_trade_journal)
    _ = mcp.tool(
        name="get_trade_journal",
        description=(
            "Query trade journals. MUST call before any sell recommendation to check "
            "thesis, hold period, target/stop prices. "
            "Returns active journals by default. "
            "Each entry includes hold_remaining_days and hold_expired."
        ),
    )(get_trade_journal)
    _ = mcp.tool(
        name="update_trade_journal",
        description=(
            "Update a trade journal. Use for: "
            "draft->active (after fill), close (target reached), stop (stop-loss hit), "
            "or adjust target/stop/notes. "
            "Find by journal_id or symbol (latest active). "
            "Auto-calculates pnl_pct on close."
        ),
    )(update_trade_journal)


__all__ = [
    "TRADE_JOURNAL_TOOL_NAMES",
    "register_trade_journal_tools",
]
```

- [ ] **Step 4: Update registry.py**

In `app/mcp_server/tooling/registry.py`, add the import:

```python
from app.mcp_server.tooling.trade_journal_registration import (
    register_trade_journal_tools,
)
```

Add to `register_all_tools()` body, after the last existing call:

```python
    register_trade_journal_tools(mcp)
```

- [ ] **Step 5: Update __init__.py**

In `app/mcp_server/tooling/__init__.py`, add the import:

```python
from app.mcp_server.tooling.trade_journal_registration import (
    TRADE_JOURNAL_TOOL_NAMES,
    register_trade_journal_tools,
)
```

Add to `__all__`:

```python
"TRADE_JOURNAL_TOOL_NAMES",
"register_trade_journal_tools",
```

- [ ] **Step 6: Update _mcp_tooling_support.py**

In `tests/_mcp_tooling_support.py`, add the import after the existing `order_execution` import:

```python
from app.mcp_server.tooling import (
    # ... existing imports ...
    trade_journal_tools,
)
```

Add `trade_journal_tools` to the `_PATCH_MODULES` tuple.

- [ ] **Step 7: Run registration tests**

Run: `uv run pytest tests/test_mcp_trade_journal.py::TestTradeJournalRegistration -v`
Expected: Both tests PASS

- [ ] **Step 8: Run all trade journal tests**

Run: `uv run pytest tests/test_mcp_trade_journal.py tests/test_trade_journal_model.py -v`
Expected: All tests PASS

- [ ] **Step 9: Commit**

```bash
git add app/mcp_server/tooling/trade_journal_registration.py app/mcp_server/tooling/registry.py app/mcp_server/tooling/__init__.py tests/_mcp_tooling_support.py tests/test_mcp_trade_journal.py
git commit -m "feat: register trade journal MCP tools"
```

---

## Task 7: Phase 1 — place_order Hook (Order Fill Recording)

**Files:**
- Modify: `app/mcp_server/tooling/order_execution.py`
- Modify: `tests/test_mcp_place_order.py`

- [ ] **Step 1: Write the failing test for order fill recording**

Append to `tests/test_mcp_place_order.py`:

```python
class TestOrderFillRecording:
    """Tests for automatic recording of order fills to review.trades."""

    @pytest.mark.asyncio
    async def test_successful_order_saves_fill(self, monkeypatch) -> None:
        """Real order execution should save to review.trades."""
        tools = build_tools()

        # Mock Upbit API calls
        monkeypatch.setattr(
            upbit_service,
            "fetch_multiple_current_prices",
            AsyncMock(return_value={"KRW-BTC": 95000000.0}),
        )
        monkeypatch.setattr(
            upbit_service,
            "fetch_my_coins",
            AsyncMock(
                return_value=[{"currency": "KRW", "balance": "500000000", "locked": "0"}]
            ),
        )
        monkeypatch.setattr(
            upbit_service,
            "place_buy_order",
            AsyncMock(
                return_value={
                    "uuid": "test-fill-uuid",
                    "side": "bid",
                    "market": "KRW-BTC",
                    "price": "95000000",
                    "volume": "0.001",
                }
            ),
        )

        # Mock the save function to capture the call
        save_mock = AsyncMock()
        monkeypatch.setattr(
            order_execution,
            "_save_order_fill",
            save_mock,
        )

        # Mock journal link
        link_mock = AsyncMock()
        monkeypatch.setattr(
            order_execution,
            "_link_journal_to_fill",
            link_mock,
        )

        result = await tools["place_order"](
            symbol="KRW-BTC",
            side="buy",
            order_type="limit",
            price=95000000.0,
            quantity=0.001,
            dry_run=False,
            reason="test fill",
        )

        assert result["success"] is True
        assert result["dry_run"] is False
        assert result.get("fill_recorded") is True
        save_mock.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_dry_run_does_not_save_fill(self, monkeypatch) -> None:
        """Dry-run orders should NOT save to review.trades."""
        tools = build_tools()

        monkeypatch.setattr(
            upbit_service,
            "fetch_multiple_current_prices",
            AsyncMock(return_value={"KRW-BTC": 95000000.0}),
        )
        monkeypatch.setattr(
            upbit_service,
            "fetch_my_coins",
            AsyncMock(
                return_value=[{"currency": "KRW", "balance": "500000000", "locked": "0"}]
            ),
        )

        save_mock = AsyncMock()
        monkeypatch.setattr(
            order_execution,
            "_save_order_fill",
            save_mock,
        )

        result = await tools["place_order"](
            symbol="KRW-BTC",
            side="buy",
            order_type="limit",
            price=95000000.0,
            quantity=0.001,
            dry_run=True,
        )

        assert result["success"] is True
        assert result["dry_run"] is True
        save_mock.assert_not_awaited()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_mcp_place_order.py::TestOrderFillRecording -v`
Expected: FAIL — `_save_order_fill` attribute not found on `order_execution`

- [ ] **Step 3: Implement `_save_order_fill` and `_link_journal_to_fill`**

Add to `app/mcp_server/tooling/order_execution.py`, before `_place_order_impl`:

```python
from sqlalchemy import select, desc
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from typing import cast as typing_cast

from app.core.db import AsyncSessionLocal
from app.core.timezone import now_kst
from app.models.review import Trade
from app.models.trade_journal import JournalStatus, TradeJournal
from app.models.trading import InstrumentType


def _order_session_factory() -> async_sessionmaker[AsyncSession]:
    return typing_cast(async_sessionmaker[AsyncSession], typing_cast(object, AsyncSessionLocal))


async def _save_order_fill(
    symbol: str,
    instrument_type: str,
    side: str,
    price: float,
    quantity: float,
    total_amount: float,
    fee: float,
    currency: str,
    account: str,
    order_id: str | None,
) -> int | None:
    """Save executed order to review.trades for permanent history.

    Returns the trade ID if inserted, None if conflict (already exists).
    """
    try:
        async with _order_session_factory()() as db:
            stmt = (
                pg_insert(Trade)
                .values(
                    trade_date=now_kst(),
                    symbol=symbol,
                    instrument_type=instrument_type,
                    side=side,
                    price=price,
                    quantity=quantity,
                    total_amount=total_amount,
                    fee=fee,
                    currency=currency,
                    account=account,
                    order_id=order_id,
                )
                .on_conflict_do_nothing(
                    constraint="uq_review_trades_account_order",
                )
            )
            result = await db.execute(stmt)
            await db.commit()

            if result.inserted_primary_key and result.inserted_primary_key[0]:
                return result.inserted_primary_key[0]
            return None
    except Exception as exc:
        logger.warning("Failed to save order fill: %s", exc)
        return None


async def _link_journal_to_fill(symbol: str, trade_id: int) -> None:
    """Link a draft journal to a fill: draft -> active, set trade_id, recalculate hold_until."""
    try:
        async with _order_session_factory()() as db:
            stmt = (
                select(TradeJournal)
                .where(
                    TradeJournal.symbol == symbol,
                    TradeJournal.status == JournalStatus.draft,
                )
                .order_by(desc(TradeJournal.created_at))
                .limit(1)
            )
            result = await db.execute(stmt)
            journal = result.scalars().first()

            if journal is None:
                return

            journal.status = JournalStatus.active
            journal.trade_id = trade_id
            if journal.min_hold_days:
                from datetime import timedelta
                journal.hold_until = now_kst() + timedelta(days=journal.min_hold_days)

            await db.commit()
            logger.info(
                "Linked journal id=%s to trade id=%s for %s",
                journal.id,
                trade_id,
                symbol,
            )
    except Exception as exc:
        logger.warning("Failed to link journal to fill: %s", exc)
```

- [ ] **Step 4: Hook into `_place_order_impl` success path**

In `app/mcp_server/tooling/order_execution.py`, modify the success return block (around line 697). Replace:

```python
        return {
            "success": True,
            "dry_run": False,
            "preview": dry_run_result,
            "execution": execution_result,
            "message": "Order placed successfully",
        }
```

With:

```python
        # Save order fill to review.trades
        fill_recorded = False
        source_map_currency = {"crypto": "KRW", "equity_kr": "KRW", "equity_us": "USD"}
        fill_currency = source_map_currency.get(market_type, "KRW")
        fill_price = float(price or current_price or 0)
        fill_quantity = float(order_quantity or 0)
        fill_amount = float(order_amount or fill_price * fill_quantity)
        fill_order_id = None
        if isinstance(execution_result, dict):
            fill_order_id = execution_result.get("uuid") or execution_result.get("order_id") or execution_result.get("ODNO")

        trade_id = await _save_order_fill(
            symbol=normalized_symbol,
            instrument_type=market_type,
            side=side_lower,
            price=fill_price,
            quantity=fill_quantity,
            total_amount=fill_amount,
            fee=0,
            currency=fill_currency,
            account=source,
            order_id=fill_order_id,
        )
        if trade_id is not None:
            fill_recorded = True
            await _link_journal_to_fill(normalized_symbol, trade_id)

        return {
            "success": True,
            "dry_run": False,
            "preview": dry_run_result,
            "execution": execution_result,
            "message": "Order placed successfully",
            "fill_recorded": fill_recorded,
        }
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_mcp_place_order.py::TestOrderFillRecording -v`
Expected: Both tests PASS

- [ ] **Step 6: Run all existing place_order tests to verify no regressions**

Run: `uv run pytest tests/test_mcp_place_order.py -v`
Expected: All tests PASS

- [ ] **Step 7: Commit**

```bash
git add app/mcp_server/tooling/order_execution.py tests/test_mcp_place_order.py
git commit -m "feat: auto-save order fills to review.trades on place_order"
```

---

## Task 8: Integration Test — Journal + Order Fill Flow

**Files:**
- Modify: `tests/test_mcp_trade_journal.py`

- [ ] **Step 1: Write end-to-end flow test**

Append to `tests/test_mcp_trade_journal.py`:

```python
class TestJournalFillIntegration:
    """Test the journal draft -> place_order -> active flow."""

    @pytest.mark.asyncio
    async def test_link_journal_to_fill_activates_draft(self) -> None:
        from app.mcp_server.tooling.order_execution import _link_journal_to_fill

        now = datetime.now(timezone.utc)
        draft_journal = TradeJournal(
            id=10,
            symbol="KRW-BTC",
            instrument_type=InstrumentType.crypto,
            thesis="RSI oversold bounce",
            status="draft",
            min_hold_days=7,
            created_at=now,
            updated_at=now,
            side="buy",
        )

        mock_session = AsyncMock()
        mock_scalars = MagicMock()
        mock_scalars.first.return_value = draft_journal
        mock_result = MagicMock()
        mock_result.scalars.return_value = mock_scalars
        mock_session.execute.return_value = mock_result

        factory = _mock_session_factory(mock_session)
        with patch(
            "app.mcp_server.tooling.order_execution._order_session_factory",
            return_value=factory,
        ):
            await _link_journal_to_fill("KRW-BTC", trade_id=42)

        assert draft_journal.status == JournalStatus.active
        assert draft_journal.trade_id == 42
        assert draft_journal.hold_until is not None

    @pytest.mark.asyncio
    async def test_link_journal_noop_when_no_draft(self) -> None:
        from app.mcp_server.tooling.order_execution import _link_journal_to_fill

        mock_session = AsyncMock()
        mock_scalars = MagicMock()
        mock_scalars.first.return_value = None
        mock_result = MagicMock()
        mock_result.scalars.return_value = mock_scalars
        mock_session.execute.return_value = mock_result

        factory = _mock_session_factory(mock_session)
        with patch(
            "app.mcp_server.tooling.order_execution._order_session_factory",
            return_value=factory,
        ):
            # Should not raise
            await _link_journal_to_fill("KRW-BTC", trade_id=42)

        mock_session.commit.assert_not_awaited()
```

- [ ] **Step 2: Run tests**

Run: `uv run pytest tests/test_mcp_trade_journal.py::TestJournalFillIntegration -v`
Expected: Both tests PASS

- [ ] **Step 3: Run full test suite**

Run: `uv run pytest tests/test_mcp_trade_journal.py tests/test_trade_journal_model.py tests/test_mcp_place_order.py -v`
Expected: All tests PASS

- [ ] **Step 4: Commit**

```bash
git add tests/test_mcp_trade_journal.py
git commit -m "test: add journal-to-fill integration tests"
```

---

## Task 9: Lint & Final Verification

**Files:** All modified files

- [ ] **Step 1: Run linter**

Run: `make lint`
Expected: No errors

- [ ] **Step 2: Run formatter**

Run: `make format`
Expected: Clean or auto-fixed

- [ ] **Step 3: Run full test suite**

Run: `make test`
Expected: All tests PASS, including the new ones

- [ ] **Step 4: Verify migration is clean**

Run: `uv run alembic check`
Expected: No pending migrations detected

- [ ] **Step 5: Final commit (if lint/format made changes)**

```bash
git add -u
git commit -m "chore: lint and format trade journal code"
```

---

## Summary of Changes

| Phase | What | Files |
|-------|------|-------|
| **Phase 1** | Auto-save order fills to `review.trades` | `order_execution.py` |
| **Phase 1** | Link draft journals to fills on execution | `order_execution.py` |
| **Phase 2** | `TradeJournal` model + migration | `trade_journal.py`, alembic |
| **Phase 2** | `save_trade_journal` tool | `trade_journal_tools.py` |
| **Phase 2** | `get_trade_journal` tool | `trade_journal_tools.py` |
| **Phase 2** | `update_trade_journal` tool | `trade_journal_tools.py` |
| **Phase 2** | MCP registration wiring | `trade_journal_registration.py`, `registry.py`, `__init__.py` |
| **Tests** | 18+ test cases across 2 files | `test_trade_journal_model.py`, `test_mcp_trade_journal.py` |
