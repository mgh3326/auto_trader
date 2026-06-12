# ROB-538 Toss Live Order Ledger Reconcile Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a Toss-specific live order ledger and evidence-gated reconcile path so Toss live orders are recorded accepted-only at send time and local fills/journals/realized PnL are booked only from `GET /orders/{orderId}` evidence.

**Architecture:** Create `review.toss_live_order_ledger` instead of extending `review.live_order_ledger`. All DB writes go through a new service layer, while MCP tooling owns broker calls and response shaping. `toss_place_order` records accepted/rejected send-time rows; `toss_modify_order` and `toss_cancel_order` record replacement/audit chain rows; `toss_reconcile_orders` fetches single-order Toss detail, classifies execution evidence, and books only broker-confirmed deltas.

**Tech Stack:** Python 3.13, SQLAlchemy async ORM, Alembic, FastMCP tooling, pytest, Ruff, ty, uv.

---

## Locked Decisions

- Use a new ORM/table: `app.models.review.TossLiveOrderLedger` mapped to `review.toss_live_order_ledger`.
- Keep `TOSS_LIVE_ORDER_MUTATIONS_ENABLED=false` operationally after this work. ROB-539 live smoke remains the enablement gate.
- Store every Toss order mutation result:
  - `operation_kind="place"` rows are fill-reconcilable.
  - `operation_kind="modify"` replacement rows are fill-reconcilable.
  - `operation_kind="cancel"` replacement rows are audit-only; reconcile can update their broker status, but must not create trade/journal rows from a cancel-operation row.
- For partial fill then cancel (`CANCELED` with `execution.filledQuantity > 0`), book the fill delta and mark the ledger row `status="cancelled"` because the order is terminal.
- For `REPLACED` with filled quantity, book the fill delta and mark the original ledger row `status="replaced"` while keeping the replacement row open.
- For `CANCEL_REJECTED` / `REPLACE_REJECTED`, record the rejection row and leave the original order row open.
- Fee booking uses Toss `commission + tax` for `review.trades.fee`; `commission`, `tax`, and `settlement_date` remain separately visible on the Toss ledger row.
- This is a high-risk live-order bookkeeping change. Apply `high_risk_change`, `needs_stronger_model_review`, and `hold_for_final_review` before merge/operational use.

## File Structure

- Modify `app/models/review.py`
  - Add `Date` import.
  - Add `TossLiveOrderLedger` ORM class.
- Modify `app/models/__init__.py`
  - Export `TossLiveOrderLedger`.
- Create `alembic/versions/20260612_rob538_toss_live_order_ledger.py`
  - Create `review.toss_live_order_ledger`.
- Create `app/services/toss_live_order_ledger_service.py`
  - Own all inserts/updates/selects for Toss ledger rows.
- Create `app/mcp_server/tooling/toss_live_evidence.py`
  - Convert Toss order detail into typed fill evidence.
- Create `app/mcp_server/tooling/toss_live_ledger.py`
  - Reconcile kernel and small MCP-facing record helpers.
- Modify `app/mcp_server/tooling/orders_toss_variants.py`
  - Wire send-time ledger writes into place/modify/cancel.
  - Register `toss_reconcile_orders`.
- Modify `app/mcp_server/README.md`
  - Update Toss tool count, ledger/reconcile contract, and safety hold text.
- Create `docs/runbooks/toss-live-order-reconcile.md`
  - Operator workflow for dry-run/apply reconcile and terminal states.
- Modify tests:
  - `tests/test_rob538_toss_live_ledger_schema.py`
  - `tests/services/test_toss_live_order_ledger_service.py`
  - `tests/mcp_server/tooling/test_toss_live_evidence.py`
  - `tests/mcp_server/tooling/test_toss_live_ledger.py`
  - `tests/test_mcp_toss_order_variants.py`

---

### Task 1: Schema And Model

**Files:**
- Modify: `app/models/review.py`
- Modify: `app/models/__init__.py`
- Create: `alembic/versions/20260612_rob538_toss_live_order_ledger.py`
- Test: `tests/test_rob538_toss_live_ledger_schema.py`

- [ ] **Step 1: Write the failing model shape test**

Create `tests/test_rob538_toss_live_ledger_schema.py`:

```python
from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


def test_toss_live_order_ledger_model_shape():
    from app.models.review import TossLiveOrderLedger

    assert TossLiveOrderLedger.__tablename__ == "toss_live_order_ledger"
    assert TossLiveOrderLedger.__table__.schema == "review"

    cols = set(TossLiveOrderLedger.__table__.columns.keys())
    for col in (
        "id",
        "trade_date",
        "client_order_id",
        "broker_order_id",
        "original_order_id",
        "replaced_by_order_id",
        "operation_kind",
        "market",
        "symbol",
        "side",
        "order_type",
        "time_in_force",
        "quantity",
        "price",
        "order_amount",
        "currency",
        "status",
        "broker_status",
        "filled_qty",
        "avg_fill_price",
        "commission",
        "tax",
        "settlement_date",
        "raw_response",
        "report_item_uuid",
        "trade_id",
        "journal_id",
        "reconciled_at",
    ):
        assert col in cols, f"missing column {col}"


def test_toss_live_order_ledger_is_exported():
    import app.models as models

    assert hasattr(models, "TossLiveOrderLedger")
```

- [ ] **Step 2: Run the schema test to verify it fails**

Run:

```bash
uv run pytest tests/test_rob538_toss_live_ledger_schema.py -q
```

Expected: FAIL with `ImportError` or `AttributeError` for `TossLiveOrderLedger`.

- [ ] **Step 3: Add the ORM model**

In `app/models/review.py`, add `Date` to the SQLAlchemy import list:

```python
from datetime import date, datetime

from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    Text,
    UniqueConstraint,
)
```

Insert this class after `LiveOrderLedger` and before `AlpacaPaperOrderLedger`:

```python
class TossLiveOrderLedger(Base):
    """ROB-538 — Toss live order lifecycle ledger.

    Toss orders are recorded accepted-only at send time. Fills, journals, and
    realized PnL are booked only by toss_reconcile_orders from GET
    /orders/{orderId} evidence.
    """

    __tablename__ = "toss_live_order_ledger"
    __table_args__ = (
        UniqueConstraint(
            "client_order_id", name="uq_toss_live_ledger_client_order_id"
        ),
        UniqueConstraint(
            "broker_order_id", name="uq_toss_live_ledger_broker_order_id"
        ),
        CheckConstraint("broker = 'toss'", name="toss_live_ledger_broker_toss"),
        CheckConstraint(
            "account_mode = 'toss_live'",
            name="toss_live_ledger_account_mode_toss_live",
        ),
        CheckConstraint(
            "operation_kind IN ('place','modify','cancel')",
            name="toss_live_ledger_operation_kind",
        ),
        CheckConstraint("market IN ('kr','us')", name="toss_live_ledger_market"),
        CheckConstraint("side IN ('buy','sell')", name="toss_live_ledger_side"),
        CheckConstraint(
            "order_type IN ('limit','market')", name="toss_live_ledger_order_type"
        ),
        CheckConstraint(
            "status IN ("
            "'accepted','rejected','pending','partial','filled','cancelled',"
            "'replaced','cancel_rejected','replace_rejected','anomaly'"
            ")",
            name="toss_live_ledger_status",
        ),
        Index("ix_toss_live_ledger_status", "status"),
        Index("ix_toss_live_ledger_market_symbol", "market", "symbol"),
        Index("ix_toss_live_ledger_broker_status", "broker_status"),
        Index("ix_toss_live_ledger_report_item_uuid", "report_item_uuid"),
        Index("ix_toss_live_ledger_replaced_by", "replaced_by_order_id"),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    trade_date: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )

    broker: Mapped[str] = mapped_column(Text, nullable=False, default="toss")
    account_mode: Mapped[str] = mapped_column(
        Text, nullable=False, default="toss_live"
    )
    operation_kind: Mapped[str] = mapped_column(Text, nullable=False)

    market: Mapped[str] = mapped_column(Text, nullable=False)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    side: Mapped[str] = mapped_column(Text, nullable=False)
    order_type: Mapped[str] = mapped_column(Text, nullable=False)
    time_in_force: Mapped[str | None] = mapped_column(Text)
    quantity: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    price: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    order_amount: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    currency: Mapped[str | None] = mapped_column(Text)

    client_order_id: Mapped[str] = mapped_column(Text, nullable=False)
    broker_order_id: Mapped[str | None] = mapped_column(Text)
    original_order_id: Mapped[str | None] = mapped_column(Text)
    replaced_by_order_id: Mapped[str | None] = mapped_column(Text)

    status: Mapped[str] = mapped_column(Text, nullable=False)
    broker_status: Mapped[str | None] = mapped_column(Text)
    response_code: Mapped[str | None] = mapped_column(Text)
    response_message: Mapped[str | None] = mapped_column(Text)
    raw_response: Mapped[dict | None] = mapped_column(JSONB)

    reason: Mapped[str | None] = mapped_column(Text)
    thesis: Mapped[str | None] = mapped_column(Text)
    strategy: Mapped[str | None] = mapped_column(Text)
    target_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    stop_loss: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    min_hold_days: Mapped[int | None] = mapped_column(SmallInteger)
    notes: Mapped[str | None] = mapped_column(Text)
    exit_reason: Mapped[str | None] = mapped_column(Text)
    indicators_snapshot: Mapped[dict | None] = mapped_column(JSONB)
    report_item_uuid: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))

    filled_qty: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    avg_fill_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    commission: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    tax: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    settlement_date: Mapped[date | None] = mapped_column(Date)
    trade_id: Mapped[int | None] = mapped_column(BigInteger)
    journal_id: Mapped[int | None] = mapped_column(BigInteger)
    reconciled_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
```

- [ ] **Step 4: Export the model**

In `app/models/__init__.py`, change the review import:

```python
from .review import (
    PendingSnapshot,
    TossLiveOrderLedger,
    Trade,
    TradeReview,
    TradeSnapshot,
)
```

Add `"TossLiveOrderLedger"` to `__all__`.

- [ ] **Step 5: Add the Alembic migration**

Create `alembic/versions/20260612_rob538_toss_live_order_ledger.py`:

```python
"""ROB-538 add Toss live order ledger.

Revision ID: 20260612_rob538_toss_live_order_ledger
Revises: 20260611_rob516_rob512_merge
Create Date: 2026-06-12
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "20260612_rob538_toss_live_order_ledger"
down_revision: Union[str, Sequence[str], None] = "20260611_rob516_rob512_merge"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "toss_live_order_ledger",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("trade_date", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("broker", sa.Text(), nullable=False),
        sa.Column("account_mode", sa.Text(), nullable=False),
        sa.Column("operation_kind", sa.Text(), nullable=False),
        sa.Column("market", sa.Text(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("side", sa.Text(), nullable=False),
        sa.Column("order_type", sa.Text(), nullable=False),
        sa.Column("time_in_force", sa.Text(), nullable=True),
        sa.Column("quantity", sa.Numeric(20, 8), nullable=True),
        sa.Column("price", sa.Numeric(20, 8), nullable=True),
        sa.Column("order_amount", sa.Numeric(20, 8), nullable=True),
        sa.Column("currency", sa.Text(), nullable=True),
        sa.Column("client_order_id", sa.Text(), nullable=False),
        sa.Column("broker_order_id", sa.Text(), nullable=True),
        sa.Column("original_order_id", sa.Text(), nullable=True),
        sa.Column("replaced_by_order_id", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("broker_status", sa.Text(), nullable=True),
        sa.Column("response_code", sa.Text(), nullable=True),
        sa.Column("response_message", sa.Text(), nullable=True),
        sa.Column("raw_response", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("thesis", sa.Text(), nullable=True),
        sa.Column("strategy", sa.Text(), nullable=True),
        sa.Column("target_price", sa.Numeric(20, 8), nullable=True),
        sa.Column("stop_loss", sa.Numeric(20, 8), nullable=True),
        sa.Column("min_hold_days", sa.SmallInteger(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("exit_reason", sa.Text(), nullable=True),
        sa.Column("indicators_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("report_item_uuid", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("filled_qty", sa.Numeric(20, 8), nullable=True),
        sa.Column("avg_fill_price", sa.Numeric(20, 8), nullable=True),
        sa.Column("commission", sa.Numeric(20, 8), nullable=True),
        sa.Column("tax", sa.Numeric(20, 8), nullable=True),
        sa.Column("settlement_date", sa.Date(), nullable=True),
        sa.Column("trade_id", sa.BigInteger(), nullable=True),
        sa.Column("journal_id", sa.BigInteger(), nullable=True),
        sa.Column("reconciled_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("broker = 'toss'", name="toss_live_ledger_broker_toss"),
        sa.CheckConstraint("account_mode = 'toss_live'", name="toss_live_ledger_account_mode_toss_live"),
        sa.CheckConstraint("operation_kind IN ('place','modify','cancel')", name="toss_live_ledger_operation_kind"),
        sa.CheckConstraint("market IN ('kr','us')", name="toss_live_ledger_market"),
        sa.CheckConstraint("side IN ('buy','sell')", name="toss_live_ledger_side"),
        sa.CheckConstraint("order_type IN ('limit','market')", name="toss_live_ledger_order_type"),
        sa.CheckConstraint(
            "status IN ('accepted','rejected','pending','partial','filled','cancelled','replaced','cancel_rejected','replace_rejected','anomaly')",
            name="toss_live_ledger_status",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_toss_live_order_ledger")),
        sa.UniqueConstraint("client_order_id", name="uq_toss_live_ledger_client_order_id"),
        sa.UniqueConstraint("broker_order_id", name="uq_toss_live_ledger_broker_order_id"),
        schema="review",
    )
    op.create_index("ix_toss_live_ledger_status", "toss_live_order_ledger", ["status"], schema="review")
    op.create_index("ix_toss_live_ledger_market_symbol", "toss_live_order_ledger", ["market", "symbol"], schema="review")
    op.create_index("ix_toss_live_ledger_broker_status", "toss_live_order_ledger", ["broker_status"], schema="review")
    op.create_index("ix_toss_live_ledger_report_item_uuid", "toss_live_order_ledger", ["report_item_uuid"], schema="review")
    op.create_index("ix_toss_live_ledger_replaced_by", "toss_live_order_ledger", ["replaced_by_order_id"], schema="review")


def downgrade() -> None:
    op.drop_index("ix_toss_live_ledger_replaced_by", table_name="toss_live_order_ledger", schema="review")
    op.drop_index("ix_toss_live_ledger_report_item_uuid", table_name="toss_live_order_ledger", schema="review")
    op.drop_index("ix_toss_live_ledger_broker_status", table_name="toss_live_order_ledger", schema="review")
    op.drop_index("ix_toss_live_ledger_market_symbol", table_name="toss_live_order_ledger", schema="review")
    op.drop_index("ix_toss_live_ledger_status", table_name="toss_live_order_ledger", schema="review")
    op.drop_table("toss_live_order_ledger", schema="review")
```

- [ ] **Step 6: Run model and Alembic checks**

Run:

```bash
uv run pytest tests/test_rob538_toss_live_order_ledger_schema.py -q
uv run alembic heads
```

Expected:
- pytest PASS.
- `alembic heads` reports `20260612_rob538_toss_live_order_ledger (head)`.

- [ ] **Step 7: Commit**

```bash
git add app/models/review.py app/models/__init__.py alembic/versions/20260612_rob538_toss_live_order_ledger.py tests/test_rob538_toss_live_ledger_schema.py
git commit -m "feat: add toss live order ledger schema"
```

---

### Task 2: Toss Ledger Service Layer

**Files:**
- Create: `app/services/toss_live_order_ledger_service.py`
- Test: `tests/services/test_toss_live_order_ledger_service.py`

- [ ] **Step 1: Write failing service tests**

Create `tests/services/test_toss_live_order_ledger_service.py`:

```python
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import delete

from app.models.review import TossLiveOrderLedger
from app.services.toss_live_order_ledger_service import TossLiveOrderLedgerService

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
async def _clean(db_session):
    await db_session.execute(delete(TossLiveOrderLedger))
    await db_session.commit()
    yield


async def test_record_place_order_is_accepted_only(db_session):
    svc = TossLiveOrderLedgerService(db_session)

    row = await svc.record_send(
        operation_kind="place",
        market="us",
        symbol="AAPL",
        side="buy",
        order_type="limit",
        time_in_force="DAY",
        quantity=Decimal("2"),
        price=Decimal("190.5"),
        order_amount=None,
        currency="USD",
        client_order_id="cid-1",
        broker_order_id="ord-1",
        original_order_id=None,
        status="accepted",
        broker_status=None,
        response_code="0",
        response_message=None,
        raw_response={"orderId": "ord-1"},
    )

    assert row.id is not None
    assert row.status == "accepted"
    assert row.filled_qty is None
    assert row.trade_id is None
    assert row.journal_id is None


async def test_mark_replaced_links_original_to_replacement(db_session):
    svc = TossLiveOrderLedgerService(db_session)
    original = await svc.record_send(
        operation_kind="place",
        market="kr",
        symbol="005930",
        side="buy",
        order_type="limit",
        time_in_force="DAY",
        quantity=Decimal("1"),
        price=Decimal("70000"),
        order_amount=None,
        currency="KRW",
        client_order_id="cid-original",
        broker_order_id="ord-original",
        original_order_id=None,
        status="accepted",
        broker_status=None,
        response_code="0",
        response_message=None,
        raw_response={},
    )

    replacement = await svc.record_send(
        operation_kind="modify",
        market="kr",
        symbol="005930",
        side="buy",
        order_type="limit",
        time_in_force="DAY",
        quantity=Decimal("1"),
        price=Decimal("70100"),
        order_amount=None,
        currency="KRW",
        client_order_id="cid-replacement",
        broker_order_id="ord-replacement",
        original_order_id="ord-original",
        status="accepted",
        broker_status=None,
        response_code="0",
        response_message=None,
        raw_response={},
    )
    await svc.mark_replaced(
        broker_order_id="ord-original",
        replaced_by_order_id="ord-replacement",
    )

    refreshed = await db_session.get(TossLiveOrderLedger, original.id)
    assert refreshed is not None
    assert refreshed.replaced_by_order_id == replacement.broker_order_id
    assert refreshed.status == "replaced"


async def test_update_reconcile_outcome_records_fee_tax_and_settlement(db_session):
    svc = TossLiveOrderLedgerService(db_session)
    row = await svc.record_send(
        operation_kind="place",
        market="us",
        symbol="AAPL",
        side="buy",
        order_type="limit",
        time_in_force="DAY",
        quantity=Decimal("2"),
        price=Decimal("190"),
        order_amount=None,
        currency="USD",
        client_order_id="cid-fill",
        broker_order_id="ord-fill",
        original_order_id=None,
        status="accepted",
        broker_status=None,
        response_code="0",
        response_message=None,
        raw_response={},
    )

    await svc.update_reconcile_outcome(
        ledger_id=row.id,
        status="filled",
        broker_status="FILLED",
        filled_qty=Decimal("2"),
        avg_fill_price=Decimal("191.25"),
        commission=Decimal("0.05"),
        tax=Decimal("0.01"),
        settlement_date=datetime(2026, 6, 15, tzinfo=UTC).date(),
        trade_id=11,
        journal_id=22,
        raw_response={"status": "FILLED"},
    )

    refreshed = await db_session.get(TossLiveOrderLedger, row.id)
    assert refreshed is not None
    assert refreshed.status == "filled"
    assert refreshed.filled_qty == Decimal("2")
    assert refreshed.commission == Decimal("0.05")
    assert refreshed.tax == Decimal("0.01")
    assert refreshed.trade_id == 11
    assert refreshed.journal_id == 22
```

- [ ] **Step 2: Run service tests to verify they fail**

```bash
uv run pytest tests/services/test_toss_live_order_ledger_service.py -q
```

Expected: FAIL because `app.services.toss_live_order_ledger_service` does not exist.

- [ ] **Step 3: Create the service**

Create `app/services/toss_live_order_ledger_service.py`:

```python
from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.review import TossLiveOrderLedger


def parse_report_item_uuid(value: str | uuid.UUID | None) -> uuid.UUID | None:
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return value
    candidate = str(value).strip()
    if not candidate:
        return None
    return uuid.UUID(candidate)


class TossLiveOrderLedgerService:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def record_send(
        self,
        *,
        operation_kind: str,
        market: str,
        symbol: str,
        side: str,
        order_type: str,
        time_in_force: str | None,
        quantity: Decimal | None,
        price: Decimal | None,
        order_amount: Decimal | None,
        currency: str | None,
        client_order_id: str,
        broker_order_id: str | None,
        original_order_id: str | None,
        status: str,
        broker_status: str | None,
        response_code: str | None,
        response_message: str | None,
        raw_response: dict[str, Any] | None,
        reason: str | None = None,
        thesis: str | None = None,
        strategy: str | None = None,
        target_price: Decimal | None = None,
        stop_loss: Decimal | None = None,
        min_hold_days: int | None = None,
        notes: str | None = None,
        exit_reason: str | None = None,
        indicators_snapshot: dict[str, Any] | None = None,
        report_item_uuid: str | uuid.UUID | None = None,
    ) -> TossLiveOrderLedger:
        row = TossLiveOrderLedger(
            trade_date=datetime.now(UTC),
            broker="toss",
            account_mode="toss_live",
            operation_kind=operation_kind,
            market=market,
            symbol=symbol,
            side=side,
            order_type=order_type,
            time_in_force=time_in_force,
            quantity=quantity,
            price=price,
            order_amount=order_amount,
            currency=currency,
            client_order_id=client_order_id,
            broker_order_id=broker_order_id,
            original_order_id=original_order_id,
            status=status,
            broker_status=broker_status,
            response_code=response_code,
            response_message=response_message,
            raw_response=raw_response,
            reason=reason,
            thesis=thesis,
            strategy=strategy,
            target_price=target_price,
            stop_loss=stop_loss,
            min_hold_days=min_hold_days,
            notes=notes,
            exit_reason=exit_reason,
            indicators_snapshot=indicators_snapshot,
            report_item_uuid=parse_report_item_uuid(report_item_uuid),
        )
        self._db.add(row)
        await self._db.flush()
        await self._db.commit()
        await self._db.refresh(row)
        return row

    async def mark_replaced(
        self, *, broker_order_id: str, replaced_by_order_id: str
    ) -> None:
        stmt = select(TossLiveOrderLedger).where(
            TossLiveOrderLedger.broker_order_id == broker_order_id
        )
        row = (await self._db.execute(stmt)).scalar_one_or_none()
        if row is None:
            return
        row.replaced_by_order_id = replaced_by_order_id
        row.status = "replaced"
        await self._db.commit()

    async def list_open(
        self,
        *,
        symbol: str | None = None,
        order_id: str | None = None,
        market: str | None = None,
        limit: int = 100,
    ) -> list[TossLiveOrderLedger]:
        stmt = select(TossLiveOrderLedger).where(
            TossLiveOrderLedger.status.in_(("accepted", "pending", "partial"))
        )
        stmt = stmt.where(TossLiveOrderLedger.operation_kind.in_(("place", "modify")))
        if symbol:
            stmt = stmt.where(TossLiveOrderLedger.symbol == symbol)
        if order_id:
            stmt = stmt.where(TossLiveOrderLedger.broker_order_id == order_id)
        if market:
            stmt = stmt.where(TossLiveOrderLedger.market == market)
        stmt = stmt.order_by(TossLiveOrderLedger.created_at.asc()).limit(limit)
        rows = list((await self._db.execute(stmt)).scalars().all())
        for row in rows:
            self._db.expunge(row)
        return rows

    async def update_reconcile_outcome(
        self,
        *,
        ledger_id: int,
        status: str,
        broker_status: str | None,
        filled_qty: Decimal | None = None,
        avg_fill_price: Decimal | None = None,
        commission: Decimal | None = None,
        tax: Decimal | None = None,
        settlement_date: date | None = None,
        trade_id: int | None = None,
        journal_id: int | None = None,
        raw_response: dict[str, Any] | None = None,
    ) -> None:
        row = await self._db.get(TossLiveOrderLedger, ledger_id)
        if row is None:
            return
        row.status = status
        row.broker_status = broker_status
        if filled_qty is not None:
            row.filled_qty = filled_qty
        if avg_fill_price is not None:
            row.avg_fill_price = avg_fill_price
        if commission is not None:
            row.commission = commission
        if tax is not None:
            row.tax = tax
        if settlement_date is not None:
            row.settlement_date = settlement_date
        if trade_id is not None:
            row.trade_id = trade_id
        if journal_id is not None:
            row.journal_id = journal_id
        if raw_response is not None:
            row.raw_response = raw_response
        row.reconciled_at = datetime.now(UTC)
        await self._db.commit()
```

- [ ] **Step 4: Run service tests**

```bash
uv run pytest tests/services/test_toss_live_order_ledger_service.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/toss_live_order_ledger_service.py tests/services/test_toss_live_order_ledger_service.py
git commit -m "feat: add toss live ledger service"
```

---

### Task 3: Toss Fill Evidence Classifier

**Files:**
- Create: `app/mcp_server/tooling/toss_live_evidence.py`
- Test: `tests/mcp_server/tooling/test_toss_live_evidence.py`

- [ ] **Step 1: Write failing evidence tests**

Create `tests/mcp_server/tooling/test_toss_live_evidence.py`:

```python
from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.mcp_server.tooling.toss_live_evidence import classify_toss_order_evidence

pytestmark = pytest.mark.unit


def _order(status: str, execution: dict | None = None):
    return SimpleNamespace(
        order_id="ord-1",
        symbol="AAPL",
        side="BUY",
        order_type="LIMIT",
        time_in_force="DAY",
        status=status,
        price=Decimal("190"),
        quantity=Decimal("2"),
        order_amount=None,
        currency="USD",
        ordered_at="2026-06-12T00:00:00Z",
        canceled_at=None,
        execution=execution or {},
    )


def test_pending_with_zero_fill_is_pending():
    evidence = classify_toss_order_evidence(_order("PENDING"))

    assert evidence.verdict == "pending"
    assert evidence.local_status == "pending"
    assert evidence.filled_qty == Decimal("0")


def test_filled_uses_execution_fee_tax_and_settlement_date():
    evidence = classify_toss_order_evidence(
        _order(
            "FILLED",
            {
                "filledQuantity": Decimal("2"),
                "averageFilledPrice": Decimal("191.25"),
                "commission": Decimal("0.05"),
                "tax": Decimal("0.01"),
                "settlementDate": "2026-06-15",
            },
        )
    )

    assert evidence.verdict == "filled"
    assert evidence.local_status == "filled"
    assert evidence.filled_qty == Decimal("2")
    assert evidence.avg_price == Decimal("191.25")
    assert evidence.fee_total == Decimal("0.06")
    assert evidence.settlement_date.isoformat() == "2026-06-15"


def test_cancelled_partial_books_delta_then_terminal_cancelled():
    evidence = classify_toss_order_evidence(
        _order(
            "CANCELED",
            {
                "filledQuantity": Decimal("0.5"),
                "averageFilledPrice": Decimal("190.5"),
                "commission": Decimal("0.02"),
                "tax": Decimal("0"),
            },
        )
    )

    assert evidence.verdict == "partial"
    assert evidence.local_status == "cancelled"
    assert evidence.filled_qty == Decimal("0.5")


def test_replaced_with_fill_books_then_terminal_replaced():
    evidence = classify_toss_order_evidence(
        _order(
            "REPLACED",
            {
                "filledQuantity": Decimal("1"),
                "averageFilledPrice": Decimal("190.5"),
            },
        )
    )

    assert evidence.verdict == "partial"
    assert evidence.local_status == "replaced"


def test_cancel_rejected_keeps_original_open_semantics():
    evidence = classify_toss_order_evidence(_order("CANCEL_REJECTED"))

    assert evidence.verdict == "pending"
    assert evidence.local_status == "cancel_rejected"


async def test_adapter_fetches_single_order_detail():
    from app.mcp_server.tooling import toss_live_evidence as ev

    class _Row:
        broker_order_id = "ord-1"

    client = SimpleNamespace(get_order=AsyncMock(return_value=_order("FILLED", {"filledQuantity": Decimal("1"), "averageFilledPrice": Decimal("10")})), aclose=AsyncMock())

    with patch.object(ev.TossReadClient, "from_settings", return_value=client):
        evidence = await ev.TossEvidenceAdapter().fetch_evidence(_Row())

    assert evidence.verdict == "filled"
    client.get_order.assert_awaited_once_with("ord-1")
    client.aclose.assert_awaited_once()
```

- [ ] **Step 2: Run evidence tests to verify they fail**

```bash
uv run pytest tests/mcp_server/tooling/test_toss_live_evidence.py -q
```

Expected: FAIL because `app.mcp_server.tooling.toss_live_evidence` does not exist.

- [ ] **Step 3: Implement the classifier and adapter**

Create `app/mcp_server/tooling/toss_live_evidence.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from app.services.brokers.toss import TossReadClient


@dataclass(frozen=True)
class TossFillEvidence:
    verdict: str
    local_status: str
    broker_status: str
    filled_qty: Decimal
    avg_price: Decimal | None
    commission: Decimal | None
    tax: Decimal | None
    fee_total: Decimal
    settlement_date: date | None
    raw_order: dict[str, Any]
    reason: str


def _to_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _to_date(value: Any) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


def _json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return f"{value.normalize():f}"
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    return value


def _raw_order(order: Any) -> dict[str, Any]:
    return {
        "orderId": getattr(order, "order_id", None),
        "symbol": getattr(order, "symbol", None),
        "side": getattr(order, "side", None),
        "orderType": getattr(order, "order_type", None),
        "timeInForce": getattr(order, "time_in_force", None),
        "status": getattr(order, "status", None),
        "price": _json_safe(getattr(order, "price", None)),
        "quantity": _json_safe(getattr(order, "quantity", None)),
        "orderAmount": _json_safe(getattr(order, "order_amount", None)),
        "currency": getattr(order, "currency", None),
        "orderedAt": getattr(order, "ordered_at", None),
        "canceledAt": getattr(order, "canceled_at", None),
        "execution": _json_safe(getattr(order, "execution", {}) or {}),
    }


def classify_toss_order_evidence(order: Any) -> TossFillEvidence:
    broker_status = str(getattr(order, "status", "") or "").upper()
    execution = dict(getattr(order, "execution", {}) or {})
    filled_qty = _to_decimal(execution.get("filledQuantity")) or Decimal("0")
    avg_price = _to_decimal(execution.get("averageFilledPrice"))
    commission = _to_decimal(execution.get("commission"))
    tax = _to_decimal(execution.get("tax"))
    fee_total = (commission or Decimal("0")) + (tax or Decimal("0"))
    settlement_date = _to_date(execution.get("settlementDate"))

    if filled_qty > 0 and avg_price and avg_price > 0:
        if broker_status == "FILLED":
            local_status = "filled"
            verdict = "filled"
        elif broker_status == "REPLACED":
            local_status = "replaced"
            verdict = "partial"
        elif broker_status == "CANCELED":
            local_status = "cancelled"
            verdict = "partial"
        elif broker_status == "PARTIAL_FILLED":
            local_status = "partial"
            verdict = "partial"
        else:
            local_status = "partial"
            verdict = "partial"
        return TossFillEvidence(
            verdict=verdict,
            local_status=local_status,
            broker_status=broker_status,
            filled_qty=filled_qty,
            avg_price=avg_price,
            commission=commission,
            tax=tax,
            fee_total=fee_total,
            settlement_date=settlement_date,
            raw_order=_raw_order(order),
            reason=f"{broker_status} {filled_qty}@{avg_price}",
        )

    if broker_status in {"PENDING", "PARTIAL_FILLED"}:
        verdict = "pending"
        local_status = "pending"
    elif broker_status == "CANCELED":
        verdict = "none"
        local_status = "cancelled"
    elif broker_status == "REJECTED":
        verdict = "none"
        local_status = "rejected"
    elif broker_status == "REPLACED":
        verdict = "none"
        local_status = "replaced"
    elif broker_status == "CANCEL_REJECTED":
        verdict = "pending"
        local_status = "cancel_rejected"
    elif broker_status == "REPLACE_REJECTED":
        verdict = "pending"
        local_status = "replace_rejected"
    else:
        verdict = "pending"
        local_status = "pending"

    return TossFillEvidence(
        verdict=verdict,
        local_status=local_status,
        broker_status=broker_status,
        filled_qty=Decimal("0"),
        avg_price=None,
        commission=commission,
        tax=tax,
        fee_total=fee_total,
        settlement_date=settlement_date,
        raw_order=_raw_order(order),
        reason=f"{broker_status} no executable fill evidence",
    )


class TossEvidenceAdapter:
    async def fetch_evidence(self, row: Any) -> TossFillEvidence:
        client = TossReadClient.from_settings()
        try:
            order = await client.get_order(str(row.broker_order_id))
            return classify_toss_order_evidence(order)
        finally:
            await client.aclose()
```

- [ ] **Step 4: Run evidence tests**

```bash
uv run pytest tests/mcp_server/tooling/test_toss_live_evidence.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/mcp_server/tooling/toss_live_evidence.py tests/mcp_server/tooling/test_toss_live_evidence.py
git commit -m "feat: classify toss live fill evidence"
```

---

### Task 4: Toss Reconcile Kernel

**Files:**
- Create: `app/mcp_server/tooling/toss_live_ledger.py`
- Test: `tests/mcp_server/tooling/test_toss_live_ledger.py`

- [ ] **Step 1: Write failing reconcile tests**

Create `tests/mcp_server/tooling/test_toss_live_ledger.py`:

```python
from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import delete

from app.models.review import TossLiveOrderLedger
from app.services.toss_live_order_ledger_service import TossLiveOrderLedgerService

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
async def _clean(db_session):
    await db_session.execute(delete(TossLiveOrderLedger))
    await db_session.commit()
    yield


async def _accepted(db_session, *, side: str = "buy"):
    return await TossLiveOrderLedgerService(db_session).record_send(
        operation_kind="place",
        market="us",
        symbol="AAPL",
        side=side,
        order_type="limit",
        time_in_force="DAY",
        quantity=Decimal("2"),
        price=Decimal("190"),
        order_amount=None,
        currency="USD",
        client_order_id=f"cid-{side}",
        broker_order_id=f"ord-{side}",
        original_order_id=None,
        status="accepted",
        broker_status=None,
        response_code="0",
        response_message=None,
        raw_response={},
        thesis="t" if side == "buy" else None,
        strategy="s" if side == "buy" else None,
        exit_reason="trim" if side == "sell" else None,
    )


async def test_reconcile_filled_buy_books_once(db_session):
    from app.mcp_server.tooling import toss_live_ledger as mod
    from app.mcp_server.tooling.toss_live_evidence import TossFillEvidence

    row = await _accepted(db_session)
    evidence = TossFillEvidence(
        verdict="filled",
        local_status="filled",
        broker_status="FILLED",
        filled_qty=Decimal("2"),
        avg_price=Decimal("191.25"),
        commission=Decimal("0.05"),
        tax=Decimal("0.01"),
        fee_total=Decimal("0.06"),
        settlement_date=None,
        raw_order={"status": "FILLED"},
        reason="filled",
    )

    class _Adapter:
        fetch_evidence = AsyncMock(return_value=evidence)

    with (
        patch.object(mod, "TossEvidenceAdapter", return_value=_Adapter()),
        patch.object(mod, "_save_order_fill", new=AsyncMock(return_value=101)) as m_fill,
        patch.object(
            mod,
            "_create_trade_journal_for_buy",
            new=AsyncMock(return_value={"journal_created": True, "journal_id": 202}),
        ) as m_journal,
        patch.object(mod, "_link_journal_to_fill", new=AsyncMock()),
    ):
        out1 = await mod._reconcile_one_toss_row(row, dry_run=False)
        row2 = await db_session.get(TossLiveOrderLedger, row.id)
        db_session.expunge(row2)
        out2 = await mod._reconcile_one_toss_row(row2, dry_run=False)

    assert out1["action"] == "booked"
    assert out2["action"] == "noop_already_booked"
    assert m_fill.await_count == 1
    assert m_fill.await_args.kwargs["fee"] == 0.06
    assert m_journal.await_count == 1


async def test_reconcile_cancelled_partial_books_delta_and_terminal(db_session):
    from app.mcp_server.tooling import toss_live_ledger as mod
    from app.mcp_server.tooling.toss_live_evidence import TossFillEvidence

    row = await _accepted(db_session)
    evidence = TossFillEvidence(
        verdict="partial",
        local_status="cancelled",
        broker_status="CANCELED",
        filled_qty=Decimal("0.5"),
        avg_price=Decimal("190.5"),
        commission=Decimal("0.02"),
        tax=Decimal("0"),
        fee_total=Decimal("0.02"),
        settlement_date=None,
        raw_order={"status": "CANCELED"},
        reason="partial cancelled",
    )

    class _Adapter:
        fetch_evidence = AsyncMock(return_value=evidence)

    with (
        patch.object(mod, "TossEvidenceAdapter", return_value=_Adapter()),
        patch.object(mod, "_save_order_fill", new=AsyncMock(return_value=303)),
        patch.object(
            mod,
            "_create_trade_journal_for_buy",
            new=AsyncMock(return_value={"journal_created": True, "journal_id": 404}),
        ),
        patch.object(mod, "_link_journal_to_fill", new=AsyncMock()),
    ):
        out = await mod._reconcile_one_toss_row(row, dry_run=False)

    assert out["action"] == "booked"
    refreshed = await db_session.get(TossLiveOrderLedger, row.id)
    assert refreshed.status == "cancelled"
    assert refreshed.filled_qty == Decimal("0.5")


async def test_reconcile_pending_is_noop(db_session):
    from app.mcp_server.tooling import toss_live_ledger as mod
    from app.mcp_server.tooling.toss_live_evidence import TossFillEvidence

    row = await _accepted(db_session)
    evidence = TossFillEvidence(
        verdict="pending",
        local_status="pending",
        broker_status="PENDING",
        filled_qty=Decimal("0"),
        avg_price=None,
        commission=None,
        tax=None,
        fee_total=Decimal("0"),
        settlement_date=None,
        raw_order={"status": "PENDING"},
        reason="pending",
    )

    class _Adapter:
        fetch_evidence = AsyncMock(return_value=evidence)

    with patch.object(mod, "TossEvidenceAdapter", return_value=_Adapter()):
        out = await mod._reconcile_one_toss_row(row, dry_run=False)

    assert out["action"] == "noop_pending"
    refreshed = await db_session.get(TossLiveOrderLedger, row.id)
    assert refreshed.status == "accepted"


async def test_reconcile_impl_lists_only_toss_rows(db_session):
    from app.mcp_server.tooling import toss_live_ledger as mod

    await _accepted(db_session)

    with patch.object(
        mod,
        "_reconcile_one_toss_row",
        new=AsyncMock(return_value={"verdict": "pending", "action": "noop_pending"}),
    ):
        out = await mod.toss_reconcile_orders_impl(dry_run=True)

    assert out["success"] is True
    assert out["dry_run"] is True
    assert out["counts"] == {"pending": 1}
```

- [ ] **Step 2: Run reconcile tests to verify they fail**

```bash
uv run pytest tests/mcp_server/tooling/test_toss_live_ledger.py -q
```

Expected: FAIL because `app.mcp_server.tooling.toss_live_ledger` does not exist.

- [ ] **Step 3: Implement the reconcile kernel**

Create `app/mcp_server/tooling/toss_live_ledger.py` with these core functions:

```python
from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

from app.mcp_server.tooling.kis_live_ledger import _order_session_factory
from app.mcp_server.tooling.order_journal import (
    _close_journals_on_sell,
    _create_trade_journal_for_buy,
    _link_journal_to_fill,
    _save_order_fill,
)
from app.mcp_server.tooling.toss_live_evidence import TossEvidenceAdapter
from app.models.review import TossLiveOrderLedger
from app.services.toss_live_order_ledger_service import TossLiveOrderLedgerService

logger = logging.getLogger(__name__)


async def _reconcile_one_toss_row(
    row: TossLiveOrderLedger, *, dry_run: bool
) -> dict[str, Any]:
    base = {
        "ledger_id": row.id,
        "order_id": row.broker_order_id,
        "client_order_id": row.client_order_id,
        "market": row.market,
        "symbol": row.symbol,
        "operation_kind": row.operation_kind,
    }
    evidence = await TossEvidenceAdapter().fetch_evidence(row)
    base["verdict"] = evidence.verdict
    base["broker_status"] = evidence.broker_status
    base["local_status"] = evidence.local_status

    if evidence.verdict == "pending":
        if evidence.local_status in {"cancel_rejected", "replace_rejected"} and not dry_run:
            async with _order_session_factory()() as db:
                await TossLiveOrderLedgerService(db).update_reconcile_outcome(
                    ledger_id=row.id,
                    status=evidence.local_status,
                    broker_status=evidence.broker_status,
                    raw_response=evidence.raw_order,
                )
        base["action"] = "noop_pending"
        return base

    if row.operation_kind == "cancel":
        base["action"] = "audit_only_cancel_row"
        if not dry_run:
            async with _order_session_factory()() as db:
                await TossLiveOrderLedgerService(db).update_reconcile_outcome(
                    ledger_id=row.id,
                    status=evidence.local_status,
                    broker_status=evidence.broker_status,
                    raw_response=evidence.raw_order,
                )
        return base

    if evidence.verdict == "none":
        base["action"] = f"marked_{evidence.local_status}"
        if not dry_run:
            async with _order_session_factory()() as db:
                await TossLiveOrderLedgerService(db).update_reconcile_outcome(
                    ledger_id=row.id,
                    status=evidence.local_status,
                    broker_status=evidence.broker_status,
                    commission=evidence.commission,
                    tax=evidence.tax,
                    settlement_date=evidence.settlement_date,
                    raw_response=evidence.raw_order,
                )
        return base

    broker_cum = evidence.filled_qty
    already = row.filled_qty or Decimal("0")
    delta = broker_cum - already
    avg_price = evidence.avg_price or Decimal("0")
    base["filled_qty"] = float(broker_cum)
    base["avg_price"] = float(avg_price)
    base["delta_qty"] = float(delta)

    if delta <= 0:
        base["action"] = "noop_already_booked"
        if not dry_run:
            async with _order_session_factory()() as db:
                await TossLiveOrderLedgerService(db).update_reconcile_outcome(
                    ledger_id=row.id,
                    status=evidence.local_status,
                    broker_status=evidence.broker_status,
                    filled_qty=broker_cum,
                    avg_fill_price=avg_price,
                    commission=evidence.commission,
                    tax=evidence.tax,
                    settlement_date=evidence.settlement_date,
                    raw_response=evidence.raw_order,
                )
        return base

    if dry_run:
        base["action"] = "would_book"
        return base

    trade_id = await _save_order_fill(
        symbol=row.symbol,
        instrument_type=("equity" if row.market == "kr" else "equity_us"),
        side=row.side,
        price=float(avg_price),
        quantity=float(delta),
        total_amount=float(avg_price) * float(delta),
        fee=float(evidence.fee_total),
        currency=row.currency or ("KRW" if row.market == "kr" else "USD"),
        account="toss",
        order_id=row.broker_order_id,
    )

    journal_id = row.journal_id
    if row.side == "buy" and row.journal_id is None:
        jr = await _create_trade_journal_for_buy(
            symbol=row.symbol,
            market_type=("equity" if row.market == "kr" else "equity_us"),
            preview={
                "price": float(avg_price),
                "quantity": float(broker_cum),
                "estimated_value": float(avg_price) * float(broker_cum),
            },
            thesis=(row.thesis or "").strip() or "toss reconciled fill",
            strategy=(row.strategy or "").strip() or "toss reconciled fill",
            target_price=float(row.target_price) if row.target_price else None,
            stop_loss=float(row.stop_loss) if row.stop_loss else None,
            min_hold_days=row.min_hold_days,
            notes=row.notes,
            indicators_snapshot=row.indicators_snapshot,
            account_type="live",
            account="toss",
        )
        journal_id = jr.get("journal_id")
        if trade_id and journal_id:
            await _link_journal_to_fill(
                symbol=row.symbol,
                trade_id=trade_id,
                account_type="live",
                account="toss",
            )
    elif row.side == "sell":
        await _close_journals_on_sell(
            symbol=row.symbol,
            sell_quantity=float(delta),
            sell_price=float(avg_price),
            exit_reason=(row.exit_reason or row.reason),
            account_type="live",
            account="toss",
        )

    async with _order_session_factory()() as db:
        await TossLiveOrderLedgerService(db).update_reconcile_outcome(
            ledger_id=row.id,
            status=evidence.local_status,
            broker_status=evidence.broker_status,
            filled_qty=broker_cum,
            avg_fill_price=avg_price,
            commission=evidence.commission,
            tax=evidence.tax,
            settlement_date=evidence.settlement_date,
            trade_id=trade_id,
            journal_id=journal_id,
            raw_response=evidence.raw_order,
        )

    base["action"] = "booked"
    base["trade_id"] = trade_id
    base["journal_id"] = journal_id
    return base


async def toss_reconcile_orders_impl(
    *,
    symbol: str | None = None,
    order_id: str | None = None,
    market: str | None = None,
    dry_run: bool = True,
    limit: int = 100,
) -> dict[str, Any]:
    async with _order_session_factory()() as db:
        rows = await TossLiveOrderLedgerService(db).list_open(
            symbol=symbol,
            order_id=order_id,
            market=market,
            limit=limit,
        )

    reconciled: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    for row in rows:
        try:
            outcome = await _reconcile_one_toss_row(row, dry_run=dry_run)
        except Exception as exc:
            logger.warning("toss reconcile failed order_id=%s: %s", row.broker_order_id, exc)
            outcome = {
                "ledger_id": row.id,
                "order_id": row.broker_order_id,
                "verdict": "anomaly",
                "error": str(exc) or exc.__class__.__name__,
            }
        reconciled.append(outcome)
        verdict = str(outcome.get("verdict", "anomaly"))
        counts[verdict] = counts.get(verdict, 0) + 1

    return {
        "success": True,
        "dry_run": dry_run,
        "counts": counts,
        "reconciled": reconciled,
        "message": f"Reconciled {len(reconciled)} Toss live order(s) (dry_run={dry_run}): {counts}",
    }
```

- [ ] **Step 4: Run reconcile tests**

```bash
uv run pytest tests/mcp_server/tooling/test_toss_live_ledger.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/mcp_server/tooling/toss_live_ledger.py tests/mcp_server/tooling/test_toss_live_ledger.py
git commit -m "feat: add toss live reconcile kernel"
```

---

### Task 5: Wire Accepted-Only Place Ledger Writes

**Files:**
- Modify: `app/mcp_server/tooling/orders_toss_variants.py`
- Test: `tests/test_mcp_toss_order_variants.py`

- [ ] **Step 1: Write failing place-ledger tests**

Append to `tests/test_mcp_toss_order_variants.py`:

```python
@pytest.mark.asyncio
async def test_place_order_records_accepted_only_toss_ledger(monkeypatch):
    import app.mcp_server.tooling.orders_toss_variants as otv
    from app.core.config import settings

    monkeypatch.setattr(settings, "toss_api_enabled", True)
    monkeypatch.setattr(otv, "validate_toss_api_config", lambda: [])

    mock_client = MockTossClient(monkeypatch)

    recorded = {}

    async def fake_record_toss_place_order(**kwargs):
        recorded.update(kwargs)
        return {
            "ledger_id": 538,
            "broker_status": "accepted",
            "fill_recorded": False,
            "journal_created": False,
        }

    monkeypatch.setattr(
        otv,
        "record_toss_place_order",
        fake_record_toss_place_order,
    )

    res = await toss_place_order(
        symbol="AAPL",
        side="buy",
        quantity="2",
        price="190",
        dry_run=False,
        confirm=True,
        account_mode="toss_live",
        thesis="entry thesis",
        strategy="swing",
        report_item_uuid="11111111-1111-1111-1111-111111111111",
    )

    assert res["success"] is True
    assert res["mutation_sent"] is True
    assert res["ledger_id"] == 538
    assert res["broker_status"] == "accepted"
    assert res["fill_recorded"] is False
    assert recorded["client_order_id"] == res["client_order_id"]
    assert recorded["broker_order_id"] == res["order_id"]
    assert recorded["thesis"] == "entry thesis"
    assert recorded["strategy"] == "swing"
    assert recorded["report_item_uuid"] == "11111111-1111-1111-1111-111111111111"
    assert mock_client.placed_payloads[0]["clientOrderId"] == res["client_order_id"]
```

- [ ] **Step 2: Run the new test to verify it fails**

```bash
uv run pytest tests/test_mcp_toss_order_variants.py::test_place_order_records_accepted_only_toss_ledger -q
```

Expected: FAIL because `toss_place_order` does not accept thesis/strategy/report metadata and does not call `record_toss_place_order`.

- [ ] **Step 3: Add record helper imports and helper function**

Near the top of `app/mcp_server/tooling/orders_toss_variants.py`, add:

```python
from app.mcp_server.tooling.toss_live_ledger import record_toss_place_order
```

This imported function will be implemented in the same module as the reconcile kernel. Add it to `app/mcp_server/tooling/toss_live_ledger.py`:

```python
async def record_toss_place_order(
    *,
    market: str,
    symbol: str,
    side: str,
    order_type: str,
    time_in_force: str,
    quantity: Decimal | None,
    price: Decimal | None,
    order_amount: Decimal | None,
    currency: str | None,
    client_order_id: str,
    broker_order_id: str | None,
    raw_response: dict[str, Any],
    reason: str | None,
    exit_reason: str | None,
    thesis: str | None,
    strategy: str | None,
    target_price: Decimal | None,
    stop_loss: Decimal | None,
    min_hold_days: int | None,
    notes: str | None,
    indicators_snapshot: dict[str, Any] | None,
    report_item_uuid: str | None,
) -> dict[str, Any]:
    status = "accepted" if broker_order_id else "rejected"
    async with _order_session_factory()() as db:
        row = await TossLiveOrderLedgerService(db).record_send(
            operation_kind="place",
            market=market,
            symbol=symbol,
            side=side,
            order_type=order_type,
            time_in_force=time_in_force,
            quantity=quantity,
            price=price,
            order_amount=order_amount,
            currency=currency,
            client_order_id=client_order_id,
            broker_order_id=broker_order_id,
            original_order_id=None,
            status=status,
            broker_status=None,
            response_code="0" if status == "accepted" else None,
            response_message=None,
            raw_response=raw_response,
            reason=reason,
            thesis=thesis,
            strategy=strategy,
            target_price=target_price,
            stop_loss=stop_loss,
            min_hold_days=min_hold_days,
            notes=notes,
            exit_reason=exit_reason,
            indicators_snapshot=indicators_snapshot,
            report_item_uuid=report_item_uuid,
        )
    return {
        "ledger_id": row.id,
        "broker_status": row.status,
        "fill_recorded": False,
        "journal_created": False,
    }
```

- [ ] **Step 4: Extend `toss_place_order` signature**

Change the `toss_place_order` signature to include optional metadata:

```python
async def toss_place_order(
    symbol: str,
    side: Literal["buy", "sell"],
    order_type: Literal["limit", "market"] = "limit",
    quantity: str | int | None = None,
    price: str | int | None = None,
    order_amount: str | int | None = None,
    market: Literal["kr", "us"] | None = None,
    time_in_force: Literal["DAY", "CLS"] = "DAY",
    dry_run: bool = True,
    confirm: bool = False,
    confirm_high_value_order: bool = False,
    reason: str | None = None,
    exit_reason: str | None = None,
    thesis: str | None = None,
    strategy: str | None = None,
    target_price: str | int | None = None,
    stop_loss: str | int | None = None,
    min_hold_days: int | None = None,
    notes: str | None = None,
    indicators_snapshot: dict[str, Any] | None = None,
    report_item_uuid: str | None = None,
    account_mode: str | None = None,
    account_type: str | None = None,
) -> dict[str, Any]:
```

Parse `target_price` and `stop_loss` after `order_amount_dec`:

```python
    target_price_dec = (
        _decimal_string(target_price, "target_price")
        if target_price is not None
        else None
    )
    stop_loss_dec = (
        _decimal_string(stop_loss, "stop_loss")
        if stop_loss is not None
        else None
    )
```

- [ ] **Step 5: Call the ledger writer after broker success**

Replace the successful return inside `execute_order` with:

```python
            res = await client.place_order(payload)
            raw_response = {
                "orderId": res.order_id,
                "clientOrderId": res.client_order_id,
                "payload": _json_safe(payload),
            }
            ledger = await record_toss_place_order(
                market=mkt,
                symbol=symbol,
                side=side,
                order_type=order_type,
                time_in_force=time_in_force,
                quantity=quantity_dec,
                price=price_dec,
                order_amount=order_amount_dec,
                currency=("KRW" if mkt == "kr" else "USD"),
                client_order_id=res.client_order_id or str(payload["clientOrderId"]),
                broker_order_id=res.order_id,
                raw_response=raw_response,
                reason=reason,
                exit_reason=exit_reason,
                thesis=thesis,
                strategy=strategy,
                target_price=target_price_dec,
                stop_loss=stop_loss_dec,
                min_hold_days=min_hold_days,
                notes=notes,
                indicators_snapshot=indicators_snapshot,
                report_item_uuid=report_item_uuid,
            )
            return {
                "success": True,
                **base_response,
                "mutation_sent": True,
                "order_id": res.order_id,
                "client_order_id": res.client_order_id,
                **ledger,
                "message": (
                    "Toss live order accepted and recorded accepted-only; "
                    "run toss_reconcile_orders to book confirmed fills."
                ),
            }
```

- [ ] **Step 6: Run place-order tests**

```bash
uv run pytest tests/test_mcp_toss_order_variants.py::test_place_order_records_accepted_only_toss_ledger -q
uv run pytest tests/test_mcp_toss_order_variants.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add app/mcp_server/tooling/orders_toss_variants.py app/mcp_server/tooling/toss_live_ledger.py tests/test_mcp_toss_order_variants.py
git commit -m "feat: record toss place orders accepted only"
```

---

### Task 6: Wire Modify And Cancel Replacement Chains

**Files:**
- Modify: `app/mcp_server/tooling/orders_toss_variants.py`
- Modify: `app/mcp_server/tooling/toss_live_ledger.py`
- Test: `tests/test_mcp_toss_order_variants.py`

- [ ] **Step 1: Write failing replacement-chain tests**

Append to `tests/test_mcp_toss_order_variants.py`:

```python
@pytest.mark.asyncio
async def test_modify_order_records_replacement_chain(monkeypatch):
    import app.mcp_server.tooling.orders_toss_variants as otv
    from app.core.config import settings

    monkeypatch.setattr(settings, "toss_api_enabled", True)
    monkeypatch.setattr(otv, "validate_toss_api_config", lambda: [])

    mock_client = MockTossClient(monkeypatch)
    mock_client.orders_list = [
        {
            "order_id": "ord-original",
            "symbol": "005930",
            "side": "BUY",
            "status": "OPEN",
            "order_type": "LIMIT",
            "time_in_force": "DAY",
            "price": Decimal("70000"),
            "quantity": Decimal("1"),
            "order_amount": None,
            "currency": "KRW",
            "ordered_at": "2026-06-12T00:00:00Z",
            "canceled_at": None,
            "execution": {},
        }
    ]

    recorded = {}

    async def fake_record_toss_replacement_order(**kwargs):
        recorded.update(kwargs)
        return {"ledger_id": 539, "broker_status": "accepted"}

    monkeypatch.setattr(
        otv,
        "record_toss_replacement_order",
        fake_record_toss_replacement_order,
    )

    res = await toss_modify_order(
        order_id="ord-original",
        new_price="70100",
        new_quantity="1",
        dry_run=False,
        confirm=True,
        account_mode="toss_live",
    )

    assert res["success"] is True
    assert res["replacement_order_id"] == "mod-ord-456"
    assert res["ledger_id"] == 539
    assert recorded["operation_kind"] == "modify"
    assert recorded["original_order_id"] == "ord-original"
    assert recorded["replacement_order_id"] == "mod-ord-456"
    assert recorded["symbol"] == "005930"


@pytest.mark.asyncio
async def test_cancel_order_records_audit_replacement_chain(monkeypatch):
    import app.mcp_server.tooling.orders_toss_variants as otv
    from app.core.config import settings

    monkeypatch.setattr(settings, "toss_api_enabled", True)
    monkeypatch.setattr(otv, "validate_toss_api_config", lambda: [])

    mock_client = MockTossClient(monkeypatch)
    mock_client.orders_list = [
        {
            "order_id": "ord-cancel-original",
            "symbol": "AAPL",
            "side": "SELL",
            "status": "OPEN",
            "order_type": "LIMIT",
            "time_in_force": "DAY",
            "price": Decimal("200"),
            "quantity": Decimal("1"),
            "order_amount": None,
            "currency": "USD",
            "ordered_at": "2026-06-12T00:00:00Z",
            "canceled_at": None,
            "execution": {},
        }
    ]

    recorded = {}

    async def fake_record_toss_replacement_order(**kwargs):
        recorded.update(kwargs)
        return {"ledger_id": 540, "broker_status": "accepted"}

    monkeypatch.setattr(
        otv,
        "record_toss_replacement_order",
        fake_record_toss_replacement_order,
    )

    res = await toss_cancel_order(
        order_id="ord-cancel-original",
        dry_run=False,
        confirm=True,
        account_mode="toss_live",
    )

    assert res["success"] is True
    assert res["replacement_order_id"] == "can-ord-789"
    assert res["ledger_id"] == 540
    assert recorded["operation_kind"] == "cancel"
    assert recorded["original_order_id"] == "ord-cancel-original"
    assert recorded["replacement_order_id"] == "can-ord-789"
```

- [ ] **Step 2: Run the new tests to verify they fail**

```bash
uv run pytest \
  tests/test_mcp_toss_order_variants.py::test_modify_order_records_replacement_chain \
  tests/test_mcp_toss_order_variants.py::test_cancel_order_records_audit_replacement_chain \
  -q
```

Expected: FAIL because `record_toss_replacement_order` is not called.

- [ ] **Step 3: Add the replacement record helper**

In `app/mcp_server/tooling/toss_live_ledger.py`, add:

```python
import uuid
```

Then add:

```python
async def record_toss_replacement_order(
    *,
    operation_kind: str,
    market: str,
    symbol: str,
    side: str,
    order_type: str,
    time_in_force: str | None,
    quantity: Decimal | None,
    price: Decimal | None,
    order_amount: Decimal | None,
    currency: str | None,
    original_order_id: str,
    replacement_order_id: str,
    raw_response: dict[str, Any],
) -> dict[str, Any]:
    async with _order_session_factory()() as db:
        svc = TossLiveOrderLedgerService(db)
        row = await svc.record_send(
            operation_kind=operation_kind,
            market=market,
            symbol=symbol,
            side=side,
            order_type=order_type,
            time_in_force=time_in_force,
            quantity=quantity,
            price=price,
            order_amount=order_amount,
            currency=currency,
            client_order_id=uuid.uuid4().hex,
            broker_order_id=replacement_order_id,
            original_order_id=original_order_id,
            status="accepted",
            broker_status=None,
            response_code="0",
            response_message=None,
            raw_response=raw_response,
        )
        await svc.mark_replaced(
            broker_order_id=original_order_id,
            replaced_by_order_id=replacement_order_id,
        )
    return {"ledger_id": row.id, "broker_status": row.status}
```

- [ ] **Step 4: Import replacement helper in Toss order tools**

In `app/mcp_server/tooling/orders_toss_variants.py`, change the import to:

```python
from app.mcp_server.tooling.toss_live_ledger import (
    record_toss_place_order,
    record_toss_replacement_order,
)
```

- [ ] **Step 5: Wire modify success**

Inside `toss_modify_order`, after `res = await client.modify_order(order_id, payload)`, add:

```python
            ledger = await record_toss_replacement_order(
                operation_kind="modify",
                market=mkt,
                symbol=symbol,
                side=side,
                order_type=orig_order_type,
                time_in_force=orig_order.time_in_force,
                quantity=new_quantity_dec or orig_order.quantity,
                price=new_price_dec or orig_order.price,
                order_amount=orig_order.order_amount,
                currency=orig_order.currency,
                original_order_id=order_id,
                replacement_order_id=res.order_id,
                raw_response={
                    "operation": "modify",
                    "originalOrderId": order_id,
                    "replacementOrderId": res.order_id,
                    "payload": _json_safe(payload),
                },
            )
```

Add `**ledger` to the success response.

- [ ] **Step 6: Wire cancel success**

Change `toss_cancel_order` so the non-dry-run path opens the client, fetches original detail, then cancels:

```python
        async with _client_context() as client:
            try:
                orig_order = await client.get_order(order_id)
            except Exception as exc:
                return _toss_error_response(exc, base_response)
            mkt = _infer_market(orig_order.symbol, None)
            res = await client.cancel_order(order_id)
            ledger = await record_toss_replacement_order(
                operation_kind="cancel",
                market=mkt,
                symbol=orig_order.symbol,
                side=str(orig_order.side).lower(),
                order_type=str(orig_order.order_type).lower(),
                time_in_force=orig_order.time_in_force,
                quantity=orig_order.quantity,
                price=orig_order.price,
                order_amount=orig_order.order_amount,
                currency=orig_order.currency,
                original_order_id=order_id,
                replacement_order_id=res.order_id,
                raw_response={
                    "operation": "cancel",
                    "originalOrderId": order_id,
                    "replacementOrderId": res.order_id,
                },
            )
            return {
                "success": True,
                **base_response,
                "mutation_sent": True,
                "original_order_id": order_id,
                "replacement_order_id": res.order_id,
                **ledger,
                "operation_semantics": "Toss cancel returns a newly issued orderId; it is not the original order id.",
            }
```

- [ ] **Step 7: Run replacement-chain tests**

```bash
uv run pytest \
  tests/test_mcp_toss_order_variants.py::test_modify_order_records_replacement_chain \
  tests/test_mcp_toss_order_variants.py::test_cancel_order_records_audit_replacement_chain \
  -q
uv run pytest tests/test_mcp_toss_order_variants.py -q
```

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add app/mcp_server/tooling/orders_toss_variants.py app/mcp_server/tooling/toss_live_ledger.py tests/test_mcp_toss_order_variants.py
git commit -m "feat: record toss modify cancel replacement chains"
```

---

### Task 7: Register `toss_reconcile_orders` And Update Docs

**Files:**
- Modify: `app/mcp_server/tooling/orders_toss_variants.py`
- Modify: `app/mcp_server/README.md`
- Create: `docs/runbooks/toss-live-order-reconcile.md`
- Test: `tests/test_mcp_toss_order_variants.py`

- [ ] **Step 1: Write failing registration test update**

In `tests/test_mcp_toss_order_variants.py`, change `test_all_seven_toss_tools_register` to:

```python
def test_all_eight_toss_tools_register():
    mcp = DummyMCP()
    register_toss_live_order_tools(mcp)
    assert set(mcp.tools.keys()) == TOSS_LIVE_ORDER_TOOL_NAMES
    assert "toss_reconcile_orders" in mcp.tools
```

In `test_toss_tool_descriptions_document_live_gates`, add:

```python
    reconcile_desc = mcp.descriptions["toss_reconcile_orders"]
    assert "dry_run=True" in reconcile_desc
    assert "GET /orders/{orderId}" in reconcile_desc
    assert "fill/journal/realized_pnl" in reconcile_desc
```

- [ ] **Step 2: Run registration tests to verify they fail**

```bash
uv run pytest \
  tests/test_mcp_toss_order_variants.py::test_all_eight_toss_tools_register \
  tests/test_mcp_toss_order_variants.py::test_toss_tool_descriptions_document_live_gates \
  -q
```

Expected: FAIL because `toss_reconcile_orders` is not registered.

- [ ] **Step 3: Register the reconcile tool**

In `app/mcp_server/tooling/orders_toss_variants.py`, add `"toss_reconcile_orders"` to `TOSS_LIVE_ORDER_TOOL_NAMES`.

Before `register_toss_live_order_tools`, add:

```python
async def toss_reconcile_orders(
    symbol: str | None = None,
    order_id: str | None = None,
    market: Literal["kr", "us"] | None = None,
    dry_run: bool = True,
    limit: int = 100,
    account_mode: str | None = None,
    account_type: str | None = None,
) -> dict[str, Any]:
    if (guard := _entry_guard(account_mode, account_type)) is not None:
        return guard
    from app.mcp_server.tooling.toss_live_ledger import toss_reconcile_orders_impl

    return await toss_reconcile_orders_impl(
        symbol=symbol,
        order_id=order_id,
        market=market,
        dry_run=dry_run,
        limit=limit,
    )
```

Inside `register_toss_live_order_tools`, add:

```python
    mcp.tool(
        name="toss_reconcile_orders",
        description=(
            "Reconcile Toss Securities live KR/US orders from the local "
            "review.toss_live_order_ledger against single-order broker evidence "
            "from GET /orders/{orderId}. Books fill/journal/realized_pnl only "
            "from confirmed execution evidence and is delta-idempotent. "
            "dry_run=True by default."
        ),
    )(toss_reconcile_orders)
```

- [ ] **Step 4: Update MCP README**

In `app/mcp_server/README.md`, update the Toss Live Order MCP Tools section:

```markdown
The `default` profile registers eight typed `toss_live` MCP tools:
- `toss_preview_order`
- `toss_place_order`
- `toss_modify_order`
- `toss_cancel_order`
- `toss_get_order_history`
- `toss_get_positions`
- `toss_get_orderable_cash`
- `toss_reconcile_orders`
```

Add a safety bullet:

```markdown
- **Accepted-only ledger and reconcile**: Real `toss_place_order` writes only an accepted/rejected row to `review.toss_live_order_ledger`. It does not create fills, journals, or realized PnL at send time. `toss_reconcile_orders(dry_run=True)` previews broker evidence from `GET /orders/{orderId}`; `dry_run=False` books only confirmed execution deltas.
```

- [ ] **Step 5: Add the runbook**

Create `docs/runbooks/toss-live-order-reconcile.md`:

```markdown
# Toss Live Order Reconcile (ROB-538)

## Contract

Toss live KR/US orders are recorded in `review.toss_live_order_ledger` at send time as accepted/rejected only. Send-time order placement never books `review.trades`, trade journals, or realized PnL.

The local bookkeeping layer is `toss_reconcile_orders`. The live-account source of truth remains Toss holdings, cash, and order detail.

## Workflow

1. Place the order with `toss_place_order(..., dry_run=False, confirm=True)`.
2. Confirm the response includes `ledger_id`, `broker_status="accepted"`, and `fill_recorded=false`.
3. Preview reconcile:

```bash
toss_reconcile_orders(dry_run=True)
```

4. Apply confirmed fills:

```bash
toss_reconcile_orders(dry_run=False)
```

5. Scope a single order when needed:

```bash
toss_reconcile_orders(order_id="ORDER_ID", dry_run=True)
toss_reconcile_orders(order_id="ORDER_ID", dry_run=False)
```

## Status Semantics

- `PENDING`: no local booking.
- `PARTIAL_FILLED`: book the new filled delta and keep the row `partial`.
- `FILLED`: book the new filled delta and mark `filled`.
- `CANCELED` with `filledQuantity > 0`: book the new filled delta and mark `cancelled`.
- `CANCELED` with `filledQuantity == 0`: mark `cancelled`, no journal side effects.
- `REPLACED` with `filledQuantity > 0`: book the new filled delta and mark the original row `replaced`; the replacement row remains reconcilable.
- `CANCEL_REJECTED` / `REPLACE_REJECTED`: record the rejected operation row and keep the original order open.

## Operational Hold

Keep `TOSS_LIVE_ORDER_MUTATIONS_ENABLED=false` until ROB-539 live smoke and stronger-model/CTO review clear this path. This feature changes live-order bookkeeping and must stay under `hold_for_final_review` until cleared.
```

- [ ] **Step 6: Run docs/registration tests**

```bash
uv run pytest tests/test_mcp_toss_order_variants.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add app/mcp_server/tooling/orders_toss_variants.py app/mcp_server/README.md docs/runbooks/toss-live-order-reconcile.md tests/test_mcp_toss_order_variants.py
git commit -m "feat: expose toss live reconcile tool"
```

---

### Task 8: Final Verification And Risk Labels

**Files:**
- No source file creation required.
- Linear issue: `ROB-538`

- [ ] **Step 1: Run focused test suite**

```bash
uv run pytest \
  tests/test_rob538_toss_live_order_ledger_schema.py \
  tests/services/test_toss_live_order_ledger_service.py \
  tests/mcp_server/tooling/test_toss_live_evidence.py \
  tests/mcp_server/tooling/test_toss_live_ledger.py \
  tests/test_mcp_toss_order_variants.py \
  -q
```

Expected: PASS.

- [ ] **Step 2: Run lint/type checks for touched modules**

```bash
uv run ruff check \
  app/models/review.py \
  app/models/__init__.py \
  app/services/toss_live_order_ledger_service.py \
  app/mcp_server/tooling/toss_live_evidence.py \
  app/mcp_server/tooling/toss_live_ledger.py \
  app/mcp_server/tooling/orders_toss_variants.py \
  tests/test_rob538_toss_live_order_ledger_schema.py \
  tests/services/test_toss_live_order_ledger_service.py \
  tests/mcp_server/tooling/test_toss_live_evidence.py \
  tests/mcp_server/tooling/test_toss_live_ledger.py \
  tests/test_mcp_toss_order_variants.py
```

Expected: PASS.

Run:

```bash
uv run ty check app tests
```

Expected: PASS or only pre-existing unrelated findings. If unrelated findings appear, record the exact files and messages in the final handoff.

- [ ] **Step 3: Check Alembic head**

```bash
uv run alembic heads
```

Expected: single head `20260612_rob538_toss_live_order_ledger`.

- [ ] **Step 4: Apply Linear risk labels and hold comment**

Use Linear tools to add these labels to `ROB-538` if they are not already present:

```text
high_risk_change
needs_stronger_model_review
hold_for_final_review
candidate_for_opus
auto_trader
Feature
```

Add this comment to `ROB-538`:

```markdown
Implementation is ready for ROB-538, but I am applying high_risk_change + needs_stronger_model_review + hold_for_final_review because this adds a live Toss order ledger, DB migration, and evidence-gated local booking path. Do not enable TOSS_LIVE_ORDER_MUTATIONS_ENABLED, deploy for operational use, or use for live Toss orders until stronger-model/CTO review clears the accepted-only ledger, replacement-chain handling, partial-cancel semantics, and reconcile idempotency.
```

- [ ] **Step 5: Commit verification notes if code changed during fixes**

If Step 1 or Step 2 required source or test fixes, commit them:

```bash
git add \
  app/models/review.py \
  app/models/__init__.py \
  app/services/toss_live_order_ledger_service.py \
  app/mcp_server/tooling/toss_live_evidence.py \
  app/mcp_server/tooling/toss_live_ledger.py \
  app/mcp_server/tooling/orders_toss_variants.py \
  app/mcp_server/README.md \
  docs/runbooks/toss-live-order-reconcile.md \
  tests/test_rob538_toss_live_order_ledger_schema.py \
  tests/services/test_toss_live_order_ledger_service.py \
  tests/mcp_server/tooling/test_toss_live_evidence.py \
  tests/mcp_server/tooling/test_toss_live_ledger.py \
  tests/test_mcp_toss_order_variants.py
git commit -m "test: verify toss live order ledger reconcile"
```

If no files changed after verification, do not create an empty commit.

---

## Self-Review Checklist

- Spec coverage:
  - New Toss-specific ledger table: Task 1.
  - Accepted-only place ledger writes: Task 5.
  - Single-order detail fill evidence: Task 3.
  - Evidence-gated reconcile: Task 4.
  - Replacement/cancel chain tracking: Task 6.
  - MCP tool and docs: Task 7.
  - High-risk hold and final verification: Task 8.
- Placeholder scan: No unresolved placeholders remain in this plan.
- Type consistency:
  - `broker_order_id` is the Toss `orderId`.
  - `client_order_id` is unique and non-null for every ledger row.
  - `operation_kind` values are `place`, `modify`, `cancel`.
  - Reconcile only books rows with `operation_kind in ("place", "modify")`.
