# ROB-298 PR 1 — Spot Demo Canonicalization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Binance Spot Demo (`demo-api.binance.com`) the canonical spot mock-trading backend with mutation-capable order execution, demo-oriented unified ledger, and full 5-mode smoke CLI; physically remove the obsolete Binance Spot Testnet active trading path. Futures Demo is deferred to PR 2 of the same issue.

**Architecture:** Unified `binance_demo_order_ledger` table with `product` discriminator (`spot` only in PR 1; `usdm_futures` reserved for PR 2). Spot Demo execution backend extends the ROB-296 preflight-only adapter with order submit/test/cancel/status via the same `BINANCE_SPOT_DEMO_*` env namespace and host allowlist (`demo-api.binance.com` only). All testnet runtime surfaces (execution_client, ledger, scalping runner, CLI scripts, tests, runbook) are physically deleted (clean-cut preference). Migration history is preserved; the testnet ledger table is dropped via forward migration only.

**Tech Stack:** Python 3.13, SQLAlchemy 2.x async, Alembic, asyncpg/PostgreSQL, pytest-asyncio, httpx, Pydantic v2. Worktree: `/Users/mgh3326/work/auto_trader.rob-298` (branch `rob-298`).

**Out of scope (do not touch in this PR):**
- USD-M Futures Demo backend (PR 2 of same issue)
- TaskIQ / Prefect / scheduler activation (ROB-292)
- Hermes/Discord integration
- Live/mainnet Binance hosts
- Other broker paths (Alpaca, KIS, Upbit, Toss)

**Operator approval scope:** Actual buy/sell smoke validation on `demo-api.binance.com` only, capped at `10 USDT` notional, with explicit CLI flag.

**Reference comment with all locked decisions:** ROB-298 comment id `d258c471-3202-444b-901b-c127f3ee44af`.

---

## File Structure

### Created

- `alembic/versions/<new_rev>_replace_testnet_with_demo_ledger.py` — forward migration: drop `binance_testnet_order_ledger`, create `binance_demo_order_ledger` with `product` discriminator
- `app/models/binance_demo_order_ledger.py` — ORM model for unified demo ledger
- `app/services/brokers/binance/demo/__init__.py` — package marker, re-exports
- `app/services/brokers/binance/demo/errors.py` — `BinanceDemoLedgerError`, `BinanceDemoInvalidStateTransition`, `BinanceDemoInvalidProduct`
- `app/services/brokers/binance/demo/ledger/__init__.py` — exports `BinanceDemoLedgerService` only (repository hidden)
- `app/services/brokers/binance/demo/ledger/repository.py` — `BinanceDemoLedgerRepository` (service-internal, AST-guarded)
- `app/services/brokers/binance/demo/ledger/service.py` — `BinanceDemoLedgerService` with `record_*` write methods + state machine validation
- `app/services/brokers/binance/spot_demo/dto.py` — `SpotDemoOrderSubmitResult`, `SpotDemoOrderTestResult`, `SpotDemoCancelResult`, `SpotDemoOpenOrdersResult`
- `app/services/brokers/binance/spot_demo/execution_client.py` — `BinanceSpotDemoExecutionClient` with `submit_order`, `order_test`, `cancel_order`, `get_open_orders`, `get_order_status`
- `app/services/brokers/binance/spot_demo/sizing.py` — `compute_demo_order_qty` (LOT_SIZE floor + MIN_NOTIONAL guard, no round-up)
- `tests/services/brokers/binance/demo/__init__.py` — empty
- `tests/services/brokers/binance/demo/test_ledger_model.py` — schema + CHECK constraints
- `tests/services/brokers/binance/demo/test_ledger_service.py` — state-machine transitions + repository import boundary guard
- `tests/services/brokers/binance/demo/test_no_testnet_imports.py` — static import guard: nothing in `app/` imports `app.services.brokers.binance.testnet.*` or `app.services.scalping.*`
- `tests/services/brokers/binance/spot_demo/test_execution_client_submit_cancel.py`
- `tests/services/brokers/binance/spot_demo/test_execution_client_fail_closed.py`
- `tests/services/brokers/binance/spot_demo/test_execution_client_order_test.py`
- `tests/services/brokers/binance/spot_demo/test_sizing.py`
- `tests/services/brokers/binance/spot_demo/test_testnet_env_does_not_activate_demo.py`
- `docs/superpowers/plans/2026-05-22-rob-298-pr1-spot-demo-canonicalization.md` — this file

### Modified

- `app/services/brokers/binance/spot_demo/__init__.py` — add execution_client export, remove `BinanceSpotDemoOrderSubmitNotImplemented`
- `app/services/brokers/binance/spot_demo/errors.py` — remove `BinanceSpotDemoOrderSubmitNotImplemented`
- `scripts/binance_spot_demo_smoke.py` — add `--order-test` and `--confirm` flags; full 5-mode handling
- `env.example` — remove testnet-related comments; document Demo as canonical
- `docs/runbooks/binance-spot-demo-smoke.md` — full lifecycle docs (preflight + order-test + confirmed-demo-order)
- `CLAUDE.md` — replace "Binance Testnet Order Ledger (ROB-286)" section with "Binance Demo Order Ledger (ROB-298)"

### Deleted

- `app/services/brokers/binance/testnet/` — entire directory (execution_client.py, transport.py, signing.py, dto.py, errors.py, host_allowlist.py, ledger/, __init__.py)
- `app/services/scalping/` — entire directory (config.py, decision.py, runner.py, notifications.py, __init__.py) — testnet-coupled
- `app/models/binance_testnet_order_ledger.py`
- `scripts/binance_testnet_lifecycle_smoke.py`
- `scripts/binance_testnet_scalper_smoke.py`
- `scripts/binance_testnet_seed_instruments.py`
- `tests/services/brokers/binance/testnet/` — entire directory
- `tests/services/scalping/` — entire directory
- `docs/runbooks/binance-testnet-scalping.md`

---

## Pre-flight (one-time setup)

- [ ] **Step P1: Verify worktree state**

Run:
```bash
cd /Users/mgh3326/work/auto_trader.rob-298
git status --short
git rev-parse --abbrev-ref HEAD
git log --oneline HEAD..origin/main | head -5
```
Expected: clean, on `rob-298`, no commits behind origin/main.

- [ ] **Step P2: Verify dependencies installed**

Run:
```bash
uv sync --all-groups
```
Expected: success or already-synced.

- [ ] **Step P3: Verify DB + Redis up**

Run:
```bash
docker compose ps | grep -E "postgres|redis"
uv run alembic current
```
Expected: services running; alembic current = `dce117125f45 (head)`.

---

## Task 1: Alembic forward migration — drop testnet ledger, create demo ledger

**Files:**
- Create: `alembic/versions/<new_rev>_replace_testnet_with_demo_ledger.py`

The migration drops `binance_testnet_order_ledger` (no data preservation — operator approval given for clean cut) and creates `binance_demo_order_ledger` with a `product` discriminator column constrained to `'spot' | 'usdm_futures'`. PR 1 only writes `'spot'` rows; PR 2 reuses the same table for futures.

- [ ] **Step 1: Generate revision file**

Run:
```bash
uv run alembic revision -m "replace testnet with demo ledger"
```
Note the generated filename + revision id. Copy them — you'll fill the body manually rather than autogenerate (we need explicit drop-then-create semantics, and autogenerate would not know about the cross-product `product` column).

- [ ] **Step 2: Write the migration body**

Replace the generated file's body with:

```python
"""replace testnet with demo ledger

Revision ID: <fill-in>
Revises: dce117125f45
Create Date: 2026-05-22 ...

ROB-298 PR 1 — drop the testnet-only ledger introduced in ROB-286 and
replace it with a unified `binance_demo_order_ledger` keyed by a
`product` discriminator ('spot' | 'usdm_futures'). PR 1 only writes
'spot' rows; PR 2 reuses the same table for futures.

No data preservation: the testnet path was operator-acknowledged as
removable (ROB-298 comment d258c471-...).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "<fill-in>"
down_revision: Union[str, Sequence[str], None] = "dce117125f45"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Drop testnet ledger first (forward-only cut).
    op.drop_index(
        "ix_binance_testnet_ledger_parent_client_order_id",
        table_name="binance_testnet_order_ledger",
    )
    op.drop_index(
        "ix_binance_testnet_ledger_created_at",
        table_name="binance_testnet_order_ledger",
    )
    op.drop_index(
        "ix_binance_testnet_ledger_lifecycle_state",
        table_name="binance_testnet_order_ledger",
    )
    op.drop_index(
        "ix_binance_testnet_ledger_broker_order_id",
        table_name="binance_testnet_order_ledger",
    )
    op.drop_index(
        "ix_binance_testnet_ledger_instrument_id",
        table_name="binance_testnet_order_ledger",
    )
    op.drop_table("binance_testnet_order_ledger")

    op.create_table(
        "binance_demo_order_ledger",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True, nullable=False),
        sa.Column(
            "instrument_id",
            sa.BigInteger(),
            sa.ForeignKey(
                "crypto_instruments.id",
                name="fk_binance_demo_ledger_instrument_id_crypto_instruments",
            ),
            nullable=False,
        ),
        sa.Column("product", sa.Text(), nullable=False),
        sa.Column("venue_host", sa.Text(), nullable=False),
        sa.Column("client_order_id", sa.Text(), nullable=False),
        sa.Column("parent_client_order_id", sa.Text(), nullable=True),
        sa.Column("broker_order_id", sa.Text(), nullable=True),
        sa.Column("side", sa.Text(), nullable=False),
        sa.Column("order_type", sa.Text(), nullable=False),
        sa.Column("qty", sa.Numeric(28, 12), nullable=False),
        sa.Column("price", sa.Numeric(28, 12), nullable=True),
        sa.Column("tp_price", sa.Numeric(28, 12), nullable=True),
        sa.Column("sl_price", sa.Numeric(28, 12), nullable=True),
        sa.Column("lifecycle_state", sa.Text(), nullable=False),
        sa.Column("planned_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("previewed_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("validated_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("submitted_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("filled_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("closed_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("cancelled_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("reconciled_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("last_reconciled_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("anomaly_reason", sa.Text(), nullable=True),
        sa.Column("anomaly_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("notional_usdt", sa.Numeric(20, 8), nullable=True),
        sa.Column("notional_override_reason", sa.Text(), nullable=True),
        sa.Column("metadata", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("client_order_id", name="uq_binance_demo_ledger_client_order_id"),
        sa.CheckConstraint(
            "product IN ('spot','usdm_futures')",
            name="binance_demo_ledger_product",
        ),
        sa.CheckConstraint(
            "lifecycle_state IN ("
            "'planned','previewed','validated','submitted','filled',"
            "'closed','cancelled','reconciled','anomaly'"
            ")",
            name="binance_demo_ledger_lifecycle_state",
        ),
        sa.CheckConstraint(
            "side IN ('BUY','SELL')",
            name="binance_demo_ledger_side",
        ),
        sa.CheckConstraint(
            "order_type IN ('LIMIT','MARKET')",
            name="binance_demo_ledger_order_type",
        ),
    )
    op.create_index(
        "ix_binance_demo_ledger_product", "binance_demo_order_ledger", ["product"]
    )
    op.create_index(
        "ix_binance_demo_ledger_instrument_id",
        "binance_demo_order_ledger",
        ["instrument_id"],
    )
    op.create_index(
        "ix_binance_demo_ledger_broker_order_id",
        "binance_demo_order_ledger",
        ["broker_order_id"],
    )
    op.create_index(
        "ix_binance_demo_ledger_lifecycle_state",
        "binance_demo_order_ledger",
        ["lifecycle_state"],
    )
    op.create_index(
        "ix_binance_demo_ledger_created_at",
        "binance_demo_order_ledger",
        ["created_at"],
    )
    op.create_index(
        "ix_binance_demo_ledger_parent_client_order_id",
        "binance_demo_order_ledger",
        ["parent_client_order_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_binance_demo_ledger_parent_client_order_id", table_name="binance_demo_order_ledger")
    op.drop_index("ix_binance_demo_ledger_created_at", table_name="binance_demo_order_ledger")
    op.drop_index("ix_binance_demo_ledger_lifecycle_state", table_name="binance_demo_order_ledger")
    op.drop_index("ix_binance_demo_ledger_broker_order_id", table_name="binance_demo_order_ledger")
    op.drop_index("ix_binance_demo_ledger_instrument_id", table_name="binance_demo_order_ledger")
    op.drop_index("ix_binance_demo_ledger_product", table_name="binance_demo_order_ledger")
    op.drop_table("binance_demo_order_ledger")
    # Note: downgrade does NOT recreate binance_testnet_order_ledger.
    # ROB-298 is a forward-only cut; rolling back would require restoring
    # the testnet model/service code as well. Use the down_revision
    # `dce117125f45` only as a marker, not for round-trip migration.


```

Lifecycle vocabulary intentionally drops `tp_sl_armed` / `tp_sl_triggered` — the new Demo ledger does not encode bracket-order legs as state, since PR 1 has no scalping runner. PR 2 (futures) decides whether to add similar states.

- [ ] **Step 3: Apply migration locally and verify**

Run:
```bash
uv run alembic upgrade head
uv run alembic current
```
Expected: current revision = your new revision id.

Then check:
```bash
docker compose exec postgres psql -U postgres -d auto_trader -c "\dt binance_*"
```
Expected: `binance_demo_order_ledger` present, `binance_testnet_order_ledger` absent.

- [ ] **Step 4: Smoke-roundtrip downgrade/upgrade**

Run:
```bash
uv run alembic downgrade -1
uv run alembic current
uv run alembic upgrade head
```
Expected: downgrade succeeds (table dropped), upgrade succeeds (table recreated, testnet table NOT recreated — confirmed by docstring note).

- [ ] **Step 5: Commit**

```bash
git add alembic/versions/*replace_testnet_with_demo_ledger*.py
git commit -m "feat(rob-298): forward migration — drop testnet ledger, create binance_demo_order_ledger"
```

---

## Task 2: ORM model for binance_demo_order_ledger

**Files:**
- Create: `app/models/binance_demo_order_ledger.py`
- Test: (covered by Task 3 schema test after model wired)

- [ ] **Step 1: Write the model**

```python
"""ROB-298 — ORM model for ``binance_demo_order_ledger``.

Unified Demo-oriented order lifecycle ledger. Keyed by a ``product``
discriminator ('spot' | 'usdm_futures'). PR 1 writes only 'spot' rows;
PR 2 adds 'usdm_futures'.

All writes must go through
``app.services.brokers.binance.demo.ledger.service.BinanceDemoLedgerService``;
the repository (``BinanceDemoLedgerRepository``) is service-internal and
guarded by an AST-scanning test (see ``test_ledger_service.py``).

State vocabulary (CHECK-constrained at DB layer):

    planned → previewed → validated → submitted → filled → closed → reconciled
    (with cancelled and anomaly branches)

Service layer enforces the transition graph; DB only validates the bag
of allowed strings.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Index,
    Numeric,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class BinanceDemoOrderLedger(Base):
    """Unified lifecycle ledger for Binance Spot Demo and USD-M Futures Demo."""

    __tablename__ = "binance_demo_order_ledger"
    __table_args__ = (
        UniqueConstraint(
            "client_order_id",
            name="uq_binance_demo_ledger_client_order_id",
        ),
        CheckConstraint(
            "product IN ('spot','usdm_futures')",
            name="binance_demo_ledger_product",
        ),
        CheckConstraint(
            "lifecycle_state IN ("
            "'planned','previewed','validated','submitted','filled',"
            "'closed','cancelled','reconciled','anomaly'"
            ")",
            name="binance_demo_ledger_lifecycle_state",
        ),
        CheckConstraint("side IN ('BUY','SELL')", name="binance_demo_ledger_side"),
        CheckConstraint(
            "order_type IN ('LIMIT','MARKET')",
            name="binance_demo_ledger_order_type",
        ),
        Index("ix_binance_demo_ledger_product", "product"),
        Index("ix_binance_demo_ledger_instrument_id", "instrument_id"),
        Index("ix_binance_demo_ledger_broker_order_id", "broker_order_id"),
        Index("ix_binance_demo_ledger_lifecycle_state", "lifecycle_state"),
        Index("ix_binance_demo_ledger_created_at", "created_at"),
        Index(
            "ix_binance_demo_ledger_parent_client_order_id",
            "parent_client_order_id",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    instrument_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey(
            "crypto_instruments.id",
            name="fk_binance_demo_ledger_instrument_id_crypto_instruments",
        ),
        nullable=False,
    )

    # Discriminator: 'spot' (PR 1) or 'usdm_futures' (PR 2).
    product: Mapped[str] = mapped_column(Text, nullable=False)

    # The host this row was written against — evidence trail to confirm
    # demo-api.binance.com vs demo-fapi.binance.com.
    venue_host: Mapped[str] = mapped_column(Text, nullable=False)

    client_order_id: Mapped[str] = mapped_column(Text, nullable=False)
    parent_client_order_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    broker_order_id: Mapped[str | None] = mapped_column(Text, nullable=True)

    side: Mapped[str] = mapped_column(Text, nullable=False)
    order_type: Mapped[str] = mapped_column(Text, nullable=False)

    qty: Mapped[Decimal] = mapped_column(Numeric(28, 12), nullable=False)
    price: Mapped[Decimal | None] = mapped_column(Numeric(28, 12), nullable=True)
    tp_price: Mapped[Decimal | None] = mapped_column(Numeric(28, 12), nullable=True)
    sl_price: Mapped[Decimal | None] = mapped_column(Numeric(28, 12), nullable=True)

    lifecycle_state: Mapped[str] = mapped_column(Text, nullable=False)

    planned_at: Mapped[dt.datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    previewed_at: Mapped[dt.datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    validated_at: Mapped[dt.datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    submitted_at: Mapped[dt.datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    filled_at: Mapped[dt.datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    closed_at: Mapped[dt.datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    cancelled_at: Mapped[dt.datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    reconciled_at: Mapped[dt.datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    last_reconciled_at: Mapped[dt.datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)

    anomaly_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    anomaly_at: Mapped[dt.datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)

    notional_usdt: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    notional_override_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    extra_metadata: Mapped[dict[str, Any] | None] = mapped_column(
        "metadata", JSONB, nullable=True
    )

    created_at: Mapped[dt.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
```

- [ ] **Step 2: Wire model into app.models registry**

Find the central models import in `app/models/__init__.py`. Add `BinanceDemoOrderLedger` next to where other models are re-exported (alphabetically after `BinanceTestnetOrderLedger` was — that entry will be removed in Task 16):

Read `app/models/__init__.py` first to confirm the import-grouping style. Add:

```python
from app.models.binance_demo_order_ledger import BinanceDemoOrderLedger
```

Add `"BinanceDemoOrderLedger"` to `__all__` if such a list exists.

- [ ] **Step 3: Verify model loads**

```bash
uv run python -c "from app.models.binance_demo_order_ledger import BinanceDemoOrderLedger; print(BinanceDemoOrderLedger.__tablename__)"
```
Expected output: `binance_demo_order_ledger`

- [ ] **Step 4: Commit**

```bash
git add app/models/binance_demo_order_ledger.py app/models/__init__.py
git commit -m "feat(rob-298): BinanceDemoOrderLedger ORM model"
```

---

## Task 3: Demo ledger errors module

**Files:**
- Create: `app/services/brokers/binance/demo/__init__.py`
- Create: `app/services/brokers/binance/demo/errors.py`

- [ ] **Step 1: Create package `__init__.py`**

```python
"""ROB-298 — Binance Demo execution domain (Spot Demo in PR 1, USD-M Futures Demo in PR 2)."""
```

- [ ] **Step 2: Create errors module**

```python
"""ROB-298 — Demo-side ledger error vocabulary.

Naming convention: ``BinanceDemo*`` for ledger/state errors that apply
across products. Per-product transport/host errors live alongside their
adapters (e.g. ``app.services.brokers.binance.spot_demo.errors``).
"""
from __future__ import annotations


class BinanceDemoLedgerError(Exception):
    """Base class for demo ledger errors."""


class BinanceDemoInvalidStateTransition(BinanceDemoLedgerError):
    """Raised when a state transition is not in the allowed graph.

    Allowed transitions (PR 1):

        planned    → previewed | cancelled | anomaly
        previewed  → validated | cancelled | anomaly
        validated  → submitted | cancelled | anomaly
        submitted  → filled    | cancelled | anomaly
        filled     → closed    | anomaly
        closed     → reconciled| anomaly
        cancelled  → reconciled| anomaly
        reconciled → (terminal)
        anomaly    → (terminal)
    """


class BinanceDemoInvalidProduct(BinanceDemoLedgerError):
    """Raised when a row's ``product`` is not in the allowed enum."""


class BinanceDemoDuplicateClientOrderId(BinanceDemoLedgerError):
    """Raised when an insert collides with an existing client_order_id."""
```

- [ ] **Step 3: Verify imports**

```bash
uv run python -c "from app.services.brokers.binance.demo.errors import BinanceDemoLedgerError, BinanceDemoInvalidStateTransition, BinanceDemoInvalidProduct, BinanceDemoDuplicateClientOrderId; print('ok')"
```
Expected: `ok`

- [ ] **Step 4: Commit**

```bash
git add app/services/brokers/binance/demo/__init__.py app/services/brokers/binance/demo/errors.py
git commit -m "feat(rob-298): demo ledger error vocabulary"
```

---

## Task 4: Demo ledger repository (service-internal)

**Files:**
- Create: `app/services/brokers/binance/demo/ledger/__init__.py`
- Create: `app/services/brokers/binance/demo/ledger/repository.py`

The repository is the only thing that hits SQLAlchemy directly. It is **not** exported from the package `__init__` — the AST-scanning test in Task 5 forbids any module outside `demo/ledger/` from importing it.

- [ ] **Step 1: Create ledger package init**

```python
"""ROB-298 — demo ledger public surface.

Only the service is exported. The repository is module-internal and
enforced by an AST-scanning test (`test_ledger_service.py`).
"""
from app.services.brokers.binance.demo.ledger.service import (
    BinanceDemoLedgerService,
)

__all__ = ["BinanceDemoLedgerService"]
```

- [ ] **Step 2: Create the repository**

```python
"""ROB-298 — Internal repository for BinanceDemoOrderLedger.

Service-internal. Never import this from outside
``app/services/brokers/binance/demo/ledger/``. The AST guard in
``tests/services/brokers/binance/demo/test_ledger_service.py``
will fail if you do.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.binance_demo_order_ledger import BinanceDemoOrderLedger


class BinanceDemoLedgerRepository:
    """Direct DB surface for the demo order ledger. Service-internal."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def insert_planned(
        self,
        *,
        instrument_id: int,
        product: str,
        venue_host: str,
        client_order_id: str,
        side: str,
        order_type: str,
        qty: Decimal,
        price: Decimal | None,
        tp_price: Decimal | None = None,
        sl_price: Decimal | None = None,
        parent_client_order_id: str | None = None,
        notional_usdt: Decimal | None = None,
        notional_override_reason: str | None = None,
        extra_metadata: dict[str, Any] | None = None,
        now: dt.datetime,
    ) -> BinanceDemoOrderLedger:
        row = BinanceDemoOrderLedger(
            instrument_id=instrument_id,
            product=product,
            venue_host=venue_host,
            client_order_id=client_order_id,
            parent_client_order_id=parent_client_order_id,
            side=side,
            order_type=order_type,
            qty=qty,
            price=price,
            tp_price=tp_price,
            sl_price=sl_price,
            lifecycle_state="planned",
            planned_at=now,
            notional_usdt=notional_usdt,
            notional_override_reason=notional_override_reason,
            extra_metadata=extra_metadata,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def get_by_client_order_id(
        self, client_order_id: str
    ) -> BinanceDemoOrderLedger | None:
        stmt = select(BinanceDemoOrderLedger).where(
            BinanceDemoOrderLedger.client_order_id == client_order_id
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def update_state(
        self,
        row: BinanceDemoOrderLedger,
        *,
        new_state: str,
        now: dt.datetime,
        broker_order_id: str | None = None,
        anomaly_reason: str | None = None,
        extra_metadata_merge: dict[str, Any] | None = None,
    ) -> BinanceDemoOrderLedger:
        row.lifecycle_state = new_state
        row.updated_at = now
        if broker_order_id is not None:
            row.broker_order_id = broker_order_id
        if anomaly_reason is not None:
            row.anomaly_reason = anomaly_reason
            row.anomaly_at = now
        if new_state == "previewed":
            row.previewed_at = now
        elif new_state == "validated":
            row.validated_at = now
        elif new_state == "submitted":
            row.submitted_at = now
        elif new_state == "filled":
            row.filled_at = now
        elif new_state == "closed":
            row.closed_at = now
        elif new_state == "cancelled":
            row.cancelled_at = now
        elif new_state == "reconciled":
            row.reconciled_at = now
            row.last_reconciled_at = now
        if extra_metadata_merge:
            merged = dict(row.extra_metadata or {})
            merged.update(extra_metadata_merge)
            row.extra_metadata = merged
        await self._session.flush()
        return row
```

- [ ] **Step 3: Verify import**

```bash
uv run python -c "from app.services.brokers.binance.demo.ledger.repository import BinanceDemoLedgerRepository; print('ok')"
```
Expected: `ok`

- [ ] **Step 4: Commit**

```bash
git add app/services/brokers/binance/demo/ledger/__init__.py app/services/brokers/binance/demo/ledger/repository.py
git commit -m "feat(rob-298): BinanceDemoLedgerRepository (service-internal)"
```

---

## Task 5: Demo ledger service + state machine + AST import guard test

**Files:**
- Create: `app/services/brokers/binance/demo/ledger/service.py`
- Create: `tests/services/brokers/binance/demo/__init__.py` (empty)
- Create: `tests/services/brokers/binance/demo/test_ledger_service.py`

- [ ] **Step 1: Write the failing test (state machine + import boundary)**

```python
"""ROB-298 — BinanceDemoLedgerService state machine + import-boundary tests."""
from __future__ import annotations

import ast
import datetime as dt
import pathlib
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.brokers.binance.demo.errors import (
    BinanceDemoInvalidProduct,
    BinanceDemoInvalidStateTransition,
)
from app.services.brokers.binance.demo.ledger import BinanceDemoLedgerService

pytestmark = pytest.mark.asyncio


async def _make_row(
    service: BinanceDemoLedgerService,
    *,
    instrument_id: int,
    product: str = "spot",
    side: str = "BUY",
) -> str:
    now = dt.datetime(2026, 5, 22, 12, 0, 0, tzinfo=dt.timezone.utc)
    client_order_id = f"test-{product}-{side}-{instrument_id}"
    await service.record_planned(
        instrument_id=instrument_id,
        product=product,
        venue_host="demo-api.binance.com",
        client_order_id=client_order_id,
        side=side,
        order_type="MARKET",
        qty=Decimal("0.001"),
        price=None,
        now=now,
    )
    return client_order_id


async def test_record_planned_creates_row(
    demo_ledger_service: BinanceDemoLedgerService,
    crypto_instrument_btc_id: int,
) -> None:
    cid = await _make_row(
        demo_ledger_service, instrument_id=crypto_instrument_btc_id
    )
    row = await demo_ledger_service.get_by_client_order_id(cid)
    assert row is not None
    assert row.product == "spot"
    assert row.lifecycle_state == "planned"
    assert row.venue_host == "demo-api.binance.com"


async def test_invalid_product_rejected(
    demo_ledger_service: BinanceDemoLedgerService,
    crypto_instrument_btc_id: int,
) -> None:
    with pytest.raises(BinanceDemoInvalidProduct):
        await _make_row(
            demo_ledger_service,
            instrument_id=crypto_instrument_btc_id,
            product="margin",
        )


async def test_state_transition_planned_to_previewed(
    demo_ledger_service: BinanceDemoLedgerService,
    crypto_instrument_btc_id: int,
) -> None:
    cid = await _make_row(
        demo_ledger_service, instrument_id=crypto_instrument_btc_id
    )
    now = dt.datetime(2026, 5, 22, 12, 0, 1, tzinfo=dt.timezone.utc)
    await demo_ledger_service.record_previewed(
        client_order_id=cid, now=now
    )
    row = await demo_ledger_service.get_by_client_order_id(cid)
    assert row.lifecycle_state == "previewed"
    assert row.previewed_at == now


async def test_invalid_state_transition_planned_to_filled(
    demo_ledger_service: BinanceDemoLedgerService,
    crypto_instrument_btc_id: int,
) -> None:
    cid = await _make_row(
        demo_ledger_service, instrument_id=crypto_instrument_btc_id
    )
    now = dt.datetime(2026, 5, 22, 12, 0, 1, tzinfo=dt.timezone.utc)
    with pytest.raises(BinanceDemoInvalidStateTransition):
        await demo_ledger_service.record_filled(
            client_order_id=cid, now=now
        )


def test_repository_import_boundary_enforced() -> None:
    """AST guard: nothing outside ``app/services/brokers/binance/demo/ledger/``
    imports ``BinanceDemoLedgerRepository``.
    """
    root = pathlib.Path("app")
    allowed_dir = pathlib.Path("app/services/brokers/binance/demo/ledger")
    offenders: list[str] = []
    for py in root.rglob("*.py"):
        if allowed_dir in py.parents or py == allowed_dir / "repository.py":
            continue
        tree = ast.parse(py.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if (
                    node.module
                    and "binance.demo.ledger.repository" in node.module
                ):
                    offenders.append(str(py))
                for alias in node.names:
                    if alias.name == "BinanceDemoLedgerRepository":
                        offenders.append(f"{py} (name import)")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if "binance.demo.ledger.repository" in alias.name:
                        offenders.append(str(py))
    assert not offenders, (
        f"BinanceDemoLedgerRepository may only be imported within "
        f"app/services/brokers/binance/demo/ledger/. Offenders: {offenders}"
    )
```

You'll need a fixture `demo_ledger_service` and `crypto_instrument_btc_id`. Add them at the top of the test file (the conftest is already async-session-aware — pattern matches `tests/services/brokers/binance/testnet/test_ledger_service.py`). Steal the fixture pattern from there before deleting that test:

```python
@pytest.fixture
async def demo_ledger_service(async_session: AsyncSession) -> BinanceDemoLedgerService:
    return BinanceDemoLedgerService(async_session)


@pytest.fixture
async def crypto_instrument_btc_id(async_session: AsyncSession) -> int:
    from app.models.crypto_instruments import CryptoInstrument
    row = CryptoInstrument(
        symbol="BTCUSDT",
        base_asset="BTC",
        quote_asset="USDT",
        exchange="binance",
        is_active=True,
    )
    async_session.add(row)
    await async_session.flush()
    return row.id
```

(If the actual CryptoInstrument model has more required columns, run `uv run python -c "from app.models.crypto_instruments import CryptoInstrument; print([c.name for c in CryptoInstrument.__table__.columns])"` first and fill them.)

- [ ] **Step 2: Run failing test**

```bash
uv run pytest tests/services/brokers/binance/demo/test_ledger_service.py -v
```
Expected: FAIL (BinanceDemoLedgerService not yet implemented).

- [ ] **Step 3: Write the service**

Create `app/services/brokers/binance/demo/ledger/service.py`:

```python
"""ROB-298 — Public write surface for the unified Binance Demo ledger.

All ledger writes go through this service. The repository is
module-internal (see `repository.py`).
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.binance_demo_order_ledger import BinanceDemoOrderLedger
from app.services.brokers.binance.demo.errors import (
    BinanceDemoInvalidProduct,
    BinanceDemoInvalidStateTransition,
)
from app.services.brokers.binance.demo.ledger.repository import (
    BinanceDemoLedgerRepository,
)

_ALLOWED_PRODUCTS = frozenset({"spot", "usdm_futures"})

_ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "planned": frozenset({"previewed", "cancelled", "anomaly"}),
    "previewed": frozenset({"validated", "cancelled", "anomaly"}),
    "validated": frozenset({"submitted", "cancelled", "anomaly"}),
    "submitted": frozenset({"filled", "cancelled", "anomaly"}),
    "filled": frozenset({"closed", "anomaly"}),
    "closed": frozenset({"reconciled", "anomaly"}),
    "cancelled": frozenset({"reconciled", "anomaly"}),
    "reconciled": frozenset(),
    "anomaly": frozenset(),
}


class BinanceDemoLedgerService:
    def __init__(self, session: AsyncSession) -> None:
        self._repo = BinanceDemoLedgerRepository(session)

    async def get_by_client_order_id(
        self, client_order_id: str
    ) -> BinanceDemoOrderLedger | None:
        return await self._repo.get_by_client_order_id(client_order_id)

    async def record_planned(
        self,
        *,
        instrument_id: int,
        product: str,
        venue_host: str,
        client_order_id: str,
        side: str,
        order_type: str,
        qty: Decimal,
        price: Decimal | None,
        tp_price: Decimal | None = None,
        sl_price: Decimal | None = None,
        parent_client_order_id: str | None = None,
        notional_usdt: Decimal | None = None,
        notional_override_reason: str | None = None,
        extra_metadata: dict[str, Any] | None = None,
        now: dt.datetime,
    ) -> BinanceDemoOrderLedger:
        if product not in _ALLOWED_PRODUCTS:
            raise BinanceDemoInvalidProduct(
                f"product={product!r} not in {sorted(_ALLOWED_PRODUCTS)}"
            )
        return await self._repo.insert_planned(
            instrument_id=instrument_id,
            product=product,
            venue_host=venue_host,
            client_order_id=client_order_id,
            side=side,
            order_type=order_type,
            qty=qty,
            price=price,
            tp_price=tp_price,
            sl_price=sl_price,
            parent_client_order_id=parent_client_order_id,
            notional_usdt=notional_usdt,
            notional_override_reason=notional_override_reason,
            extra_metadata=extra_metadata,
            now=now,
        )

    async def _transition(
        self,
        *,
        client_order_id: str,
        new_state: str,
        now: dt.datetime,
        broker_order_id: str | None = None,
        anomaly_reason: str | None = None,
        extra_metadata_merge: dict[str, Any] | None = None,
    ) -> BinanceDemoOrderLedger:
        row = await self._repo.get_by_client_order_id(client_order_id)
        if row is None:
            raise BinanceDemoInvalidStateTransition(
                f"no ledger row for client_order_id={client_order_id!r}"
            )
        if new_state not in _ALLOWED_TRANSITIONS.get(row.lifecycle_state, frozenset()):
            raise BinanceDemoInvalidStateTransition(
                f"{row.lifecycle_state!r} → {new_state!r} not allowed"
            )
        return await self._repo.update_state(
            row,
            new_state=new_state,
            now=now,
            broker_order_id=broker_order_id,
            anomaly_reason=anomaly_reason,
            extra_metadata_merge=extra_metadata_merge,
        )

    async def record_previewed(self, *, client_order_id: str, now: dt.datetime, extra_metadata_merge: dict[str, Any] | None = None) -> BinanceDemoOrderLedger:
        return await self._transition(client_order_id=client_order_id, new_state="previewed", now=now, extra_metadata_merge=extra_metadata_merge)

    async def record_validated(self, *, client_order_id: str, now: dt.datetime, extra_metadata_merge: dict[str, Any] | None = None) -> BinanceDemoOrderLedger:
        return await self._transition(client_order_id=client_order_id, new_state="validated", now=now, extra_metadata_merge=extra_metadata_merge)

    async def record_submitted(self, *, client_order_id: str, broker_order_id: str, now: dt.datetime, extra_metadata_merge: dict[str, Any] | None = None) -> BinanceDemoOrderLedger:
        return await self._transition(client_order_id=client_order_id, new_state="submitted", broker_order_id=broker_order_id, now=now, extra_metadata_merge=extra_metadata_merge)

    async def record_filled(self, *, client_order_id: str, now: dt.datetime, extra_metadata_merge: dict[str, Any] | None = None) -> BinanceDemoOrderLedger:
        return await self._transition(client_order_id=client_order_id, new_state="filled", now=now, extra_metadata_merge=extra_metadata_merge)

    async def record_closed(self, *, client_order_id: str, now: dt.datetime, extra_metadata_merge: dict[str, Any] | None = None) -> BinanceDemoOrderLedger:
        return await self._transition(client_order_id=client_order_id, new_state="closed", now=now, extra_metadata_merge=extra_metadata_merge)

    async def record_cancelled(self, *, client_order_id: str, now: dt.datetime, extra_metadata_merge: dict[str, Any] | None = None) -> BinanceDemoOrderLedger:
        return await self._transition(client_order_id=client_order_id, new_state="cancelled", now=now, extra_metadata_merge=extra_metadata_merge)

    async def record_reconciled(self, *, client_order_id: str, now: dt.datetime, extra_metadata_merge: dict[str, Any] | None = None) -> BinanceDemoOrderLedger:
        return await self._transition(client_order_id=client_order_id, new_state="reconciled", now=now, extra_metadata_merge=extra_metadata_merge)

    async def record_anomaly(self, *, client_order_id: str, reason: str, now: dt.datetime, extra_metadata_merge: dict[str, Any] | None = None) -> BinanceDemoOrderLedger:
        return await self._transition(client_order_id=client_order_id, new_state="anomaly", anomaly_reason=reason, now=now, extra_metadata_merge=extra_metadata_merge)
```

- [ ] **Step 4: Run tests to verify pass**

```bash
uv run pytest tests/services/brokers/binance/demo/test_ledger_service.py -v
```
Expected: all PASS, including AST import-boundary test.

- [ ] **Step 5: Commit**

```bash
git add app/services/brokers/binance/demo/ledger/service.py tests/services/brokers/binance/demo/
git commit -m "feat(rob-298): BinanceDemoLedgerService with state machine + AST guard"
```

---

## Task 6: Spot Demo DTOs

**Files:**
- Create: `app/services/brokers/binance/spot_demo/dto.py`

- [ ] **Step 1: Write DTOs**

```python
"""ROB-298 — DTOs for Spot Demo execution backend responses."""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class SpotDemoOrderSubmitResult:
    client_order_id: str
    broker_order_id: str
    symbol: str
    side: str
    order_type: str
    qty: Decimal
    executed_qty: Decimal
    cummulative_quote_qty: Decimal
    status: str  # FILLED / PARTIALLY_FILLED / NEW / ...
    raw_response_redacted: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SpotDemoOrderTestResult:
    """`/api/v3/order/test` returned 200 with an empty body (success)."""

    symbol: str
    side: str
    order_type: str
    qty: Decimal


@dataclass(frozen=True)
class SpotDemoCancelResult:
    client_order_id: str
    broker_order_id: str
    symbol: str
    status: str
    raw_response_redacted: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SpotDemoOpenOrder:
    client_order_id: str
    broker_order_id: str
    symbol: str
    side: str
    qty: Decimal
    status: str


@dataclass(frozen=True)
class SpotDemoOpenOrdersResult:
    orders: list[SpotDemoOpenOrder]
```

- [ ] **Step 2: Verify imports**

```bash
uv run python -c "from app.services.brokers.binance.spot_demo.dto import SpotDemoOrderSubmitResult, SpotDemoOrderTestResult, SpotDemoCancelResult, SpotDemoOpenOrdersResult; print('ok')"
```
Expected: `ok`

- [ ] **Step 3: Commit**

```bash
git add app/services/brokers/binance/spot_demo/dto.py
git commit -m "feat(rob-298): Spot Demo execution DTOs"
```

---

## Task 7: Spot Demo sizing helper

**Files:**
- Create: `app/services/brokers/binance/spot_demo/sizing.py`
- Test: `tests/services/brokers/binance/spot_demo/test_sizing.py`

Sizing rule (locked in ROB-298 comment d258c471):
- order notional must satisfy `MIN_NOTIONAL ≤ notional ≤ cap` (cap = 10 USDT default)
- quantity = floor(target_notional / price / step_size) × step_size
- if floor result × price < MIN_NOTIONAL → return `BlockedSizing` (do NOT round up)

- [ ] **Step 1: Write the failing test**

```python
"""ROB-298 — Spot Demo sizing helper: floor to LOT_SIZE, never round up."""
from __future__ import annotations

from decimal import Decimal

import pytest

from app.services.brokers.binance.spot_demo.sizing import (
    SizingBlocked,
    SizingResult,
    compute_demo_order_qty,
)


def test_floor_to_step_size() -> None:
    result = compute_demo_order_qty(
        target_notional_usdt=Decimal("10"),
        price=Decimal("100"),
        min_notional=Decimal("5"),
        step_size=Decimal("0.001"),
        cap_usdt=Decimal("10"),
    )
    assert isinstance(result, SizingResult)
    # target qty = 10/100 = 0.1; floor to 0.001 step = 0.100
    assert result.qty == Decimal("0.100")
    assert result.notional_usdt == Decimal("10.000")


def test_blocked_when_floor_below_min_notional() -> None:
    # price=$100, step=1.0, min_notional=$50, cap=$10
    # target qty = 10/100 = 0.1, floor to 1.0 step = 0 → blocked
    result = compute_demo_order_qty(
        target_notional_usdt=Decimal("10"),
        price=Decimal("100"),
        min_notional=Decimal("50"),
        step_size=Decimal("1.0"),
        cap_usdt=Decimal("10"),
    )
    assert isinstance(result, SizingBlocked)
    assert "MIN_NOTIONAL" in result.reason


def test_never_rounds_up_past_cap() -> None:
    # target $10 cap, step=0.01, price=$3 → floor qty=3.33; notional=9.99 ≤ cap
    result = compute_demo_order_qty(
        target_notional_usdt=Decimal("10"),
        price=Decimal("3"),
        min_notional=Decimal("5"),
        step_size=Decimal("0.01"),
        cap_usdt=Decimal("10"),
    )
    assert isinstance(result, SizingResult)
    assert result.notional_usdt <= Decimal("10")


def test_target_above_cap_clipped_to_cap() -> None:
    result = compute_demo_order_qty(
        target_notional_usdt=Decimal("20"),
        price=Decimal("100"),
        min_notional=Decimal("5"),
        step_size=Decimal("0.001"),
        cap_usdt=Decimal("10"),
    )
    assert isinstance(result, SizingResult)
    assert result.notional_usdt <= Decimal("10")


def test_cap_must_be_positive() -> None:
    with pytest.raises(ValueError):
        compute_demo_order_qty(
            target_notional_usdt=Decimal("10"),
            price=Decimal("100"),
            min_notional=Decimal("5"),
            step_size=Decimal("0.001"),
            cap_usdt=Decimal("0"),
        )
```

- [ ] **Step 2: Run failing test**

```bash
uv run pytest tests/services/brokers/binance/spot_demo/test_sizing.py -v
```
Expected: FAIL (sizing module not present).

- [ ] **Step 3: Write the implementation**

```python
"""ROB-298 — Spot Demo order sizing helper.

Computes ``qty`` from a target USDT notional under Binance Spot
exchangeInfo filters (``LOT_SIZE.stepSize`` + ``MIN_NOTIONAL``) and a
ROB-298 max cap. Always floors to step; never rounds up. If floored
quantity violates ``MIN_NOTIONAL``, returns ``SizingBlocked``.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal


@dataclass(frozen=True)
class SizingResult:
    qty: Decimal
    notional_usdt: Decimal


@dataclass(frozen=True)
class SizingBlocked:
    reason: str


def compute_demo_order_qty(
    *,
    target_notional_usdt: Decimal,
    price: Decimal,
    min_notional: Decimal,
    step_size: Decimal,
    cap_usdt: Decimal,
) -> SizingResult | SizingBlocked:
    if cap_usdt <= 0:
        raise ValueError("cap_usdt must be > 0")
    if price <= 0:
        raise ValueError("price must be > 0")
    if step_size <= 0:
        raise ValueError("step_size must be > 0")

    effective_target = min(target_notional_usdt, cap_usdt)
    raw_qty = effective_target / price
    floored_qty = (raw_qty / step_size).quantize(Decimal("1"), rounding=ROUND_DOWN) * step_size
    if floored_qty <= 0:
        return SizingBlocked(
            reason=f"floored qty=0 (target={effective_target} / price={price} < step_size={step_size})"
        )
    notional = floored_qty * price
    if notional < min_notional:
        return SizingBlocked(
            reason=f"notional={notional} < MIN_NOTIONAL={min_notional} after LOT_SIZE floor (qty={floored_qty})"
        )
    if notional > cap_usdt:
        # Defense in depth: floor should never go above cap; trip an
        # assertion-equivalent guard rather than silently send.
        return SizingBlocked(
            reason=f"computed notional={notional} > cap={cap_usdt} (sizing bug)"
        )
    return SizingResult(qty=floored_qty, notional_usdt=notional)
```

- [ ] **Step 4: Run tests to verify pass**

```bash
uv run pytest tests/services/brokers/binance/spot_demo/test_sizing.py -v
```
Expected: 5 PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/brokers/binance/spot_demo/sizing.py tests/services/brokers/binance/spot_demo/test_sizing.py
git commit -m "feat(rob-298): Spot Demo sizing helper (LOT_SIZE floor, no round-up)"
```

---

## Task 8: Spot Demo execution client (submit / order-test / cancel / status)

**Files:**
- Create: `app/services/brokers/binance/spot_demo/execution_client.py`
- Modify: `app/services/brokers/binance/spot_demo/errors.py` (remove `BinanceSpotDemoOrderSubmitNotImplemented`)
- Modify: `app/services/brokers/binance/spot_demo/__init__.py` (export execution client)
- Test: `tests/services/brokers/binance/spot_demo/test_execution_client_submit_cancel.py`
- Test: `tests/services/brokers/binance/spot_demo/test_execution_client_order_test.py`
- Test: `tests/services/brokers/binance/spot_demo/test_execution_client_fail_closed.py`

The execution client reuses the existing `spot_demo/transport.py` (signed HMAC, host-allowlist enforced) and `spot_demo/signing.py`. Pattern mirrors what the deleted testnet `execution_client.py` did, but with stricter Demo-only invariants.

**Before writing**, read these existing files so you can match their style:
- `app/services/brokers/binance/spot_demo/preflight.py` (factory + env reading pattern)
- `app/services/brokers/binance/spot_demo/transport.py` (request envelope)
- `app/services/brokers/binance/spot_demo/host_allowlist.py` (`assert_spot_demo_host`)
- `app/services/brokers/binance/testnet/execution_client.py` — **read before deletion** to copy the operator-gate pattern (`confirm=True` per call, default = dry-run)

- [ ] **Step 1: Write failing tests (fail-closed)**

`tests/services/brokers/binance/spot_demo/test_execution_client_fail_closed.py`:

```python
"""ROB-298 — BinanceSpotDemoExecutionClient must fail-closed in all unsafe modes."""
from __future__ import annotations

import pytest

from app.services.brokers.binance.spot_demo.errors import (
    BinanceSpotDemoDisabled,
    BinanceSpotDemoMissingCredentials,
)
from app.services.brokers.binance.spot_demo.execution_client import (
    BinanceSpotDemoExecutionClient,
)


def test_disabled_when_env_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BINANCE_SPOT_DEMO_ENABLED", "false")
    monkeypatch.setenv("BINANCE_SPOT_DEMO_API_KEY", "key")
    monkeypatch.setenv("BINANCE_SPOT_DEMO_API_SECRET", "secret")
    with pytest.raises(BinanceSpotDemoDisabled):
        BinanceSpotDemoExecutionClient.from_env()


def test_disabled_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BINANCE_SPOT_DEMO_ENABLED", raising=False)
    with pytest.raises(BinanceSpotDemoDisabled):
        BinanceSpotDemoExecutionClient.from_env()


def test_missing_credentials_when_enabled_without_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BINANCE_SPOT_DEMO_ENABLED", "true")
    monkeypatch.delenv("BINANCE_SPOT_DEMO_API_KEY", raising=False)
    monkeypatch.delenv("BINANCE_SPOT_DEMO_API_SECRET", raising=False)
    with pytest.raises(BinanceSpotDemoMissingCredentials):
        BinanceSpotDemoExecutionClient.from_env()


def test_base_url_must_be_demo_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BINANCE_SPOT_DEMO_ENABLED", "true")
    monkeypatch.setenv("BINANCE_SPOT_DEMO_API_KEY", "key")
    monkeypatch.setenv("BINANCE_SPOT_DEMO_API_SECRET", "secret")
    monkeypatch.setenv("BINANCE_SPOT_DEMO_BASE_URL", "https://api.binance.com")
    with pytest.raises(Exception):
        # Either BinanceSpotDemoCrossAllowlistViolation or similar
        BinanceSpotDemoExecutionClient.from_env()
```

`tests/services/brokers/binance/spot_demo/test_execution_client_submit_cancel.py`:

```python
"""ROB-298 — submit_order returns submit result only with confirm=True; default = DryRunResult."""
from __future__ import annotations

from decimal import Decimal

import pytest

from app.services.brokers.binance.spot_demo.execution_client import (
    BinanceSpotDemoExecutionClient,
    SpotDemoDryRunResult,
)


@pytest.fixture
def enabled_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BINANCE_SPOT_DEMO_ENABLED", "true")
    monkeypatch.setenv("BINANCE_SPOT_DEMO_API_KEY", "key")
    monkeypatch.setenv("BINANCE_SPOT_DEMO_API_SECRET", "secret")
    monkeypatch.setenv("BINANCE_SPOT_DEMO_BASE_URL", "https://demo-api.binance.com")


def test_submit_order_default_returns_dry_run(enabled_env: None) -> None:
    client = BinanceSpotDemoExecutionClient.from_env()
    result = client.preview_submit(
        symbol="BTCUSDT",
        side="BUY",
        order_type="MARKET",
        qty=Decimal("0.0001"),
        client_order_id="test-cid-1",
    )
    assert isinstance(result, SpotDemoDryRunResult)
    assert result.symbol == "BTCUSDT"


async def test_submit_order_requires_confirm_for_http(
    enabled_env: None, httpx_mock
) -> None:
    """Verifies that without confirm=True, no HTTP request is dispatched."""
    client = BinanceSpotDemoExecutionClient.from_env()
    # httpx_mock has no registered handlers; any HTTP would raise
    result = client.preview_submit(
        symbol="BTCUSDT",
        side="BUY",
        order_type="MARKET",
        qty=Decimal("0.0001"),
        client_order_id="test-cid-2",
    )
    assert isinstance(result, SpotDemoDryRunResult)
    # No HTTP calls dispatched (httpx_mock.get_requests() is empty)
    assert httpx_mock.get_requests() == []
```

`tests/services/brokers/binance/spot_demo/test_execution_client_order_test.py`:

```python
"""ROB-298 — order_test mode hits /api/v3/order/test, not /api/v3/order."""
from __future__ import annotations

from decimal import Decimal

import pytest

from app.services.brokers.binance.spot_demo.execution_client import (
    BinanceSpotDemoExecutionClient,
)


@pytest.fixture
def enabled_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BINANCE_SPOT_DEMO_ENABLED", "true")
    monkeypatch.setenv("BINANCE_SPOT_DEMO_API_KEY", "key")
    monkeypatch.setenv("BINANCE_SPOT_DEMO_API_SECRET", "secret")
    monkeypatch.setenv("BINANCE_SPOT_DEMO_BASE_URL", "https://demo-api.binance.com")


async def test_order_test_hits_order_test_path(
    enabled_env: None, httpx_mock
) -> None:
    httpx_mock.add_response(
        url__regex=r"https://demo-api\.binance\.com/api/v3/order/test\?.*",
        method="POST",
        status_code=200,
        json={},
    )
    client = BinanceSpotDemoExecutionClient.from_env()
    result = await client.order_test(
        symbol="BTCUSDT",
        side="BUY",
        order_type="MARKET",
        qty=Decimal("0.0001"),
    )
    assert result.symbol == "BTCUSDT"
    requests = httpx_mock.get_requests()
    assert any("/api/v3/order/test" in str(r.url) for r in requests)
    assert not any(str(r.url).endswith("/api/v3/order") for r in requests)
```

- [ ] **Step 2: Run failing tests**

```bash
uv run pytest tests/services/brokers/binance/spot_demo/test_execution_client_*.py -v
```
Expected: FAIL (execution_client not yet implemented).

- [ ] **Step 3: Remove the `BinanceSpotDemoOrderSubmitNotImplemented` error**

Open `app/services/brokers/binance/spot_demo/errors.py` and delete the `BinanceSpotDemoOrderSubmitNotImplemented` class entirely (it was a placeholder under ROB-296). Other errors stay.

- [ ] **Step 4: Implement `BinanceSpotDemoExecutionClient`**

Write `app/services/brokers/binance/spot_demo/execution_client.py`. Use the deleted testnet `execution_client.py` as a structural reference but enforce:
- `BINANCE_SPOT_DEMO_*` env namespace only (never read `BINANCE_TESTNET_*`)
- base URL host must pass `assert_spot_demo_host` (i.e., `demo-api.binance.com` only)
- `preview_submit(...)` returns `SpotDemoDryRunResult` without HTTP
- `submit_order(..., confirm=True)` is the only call that dispatches `POST /api/v3/order`
- `order_test(...)` hits `POST /api/v3/order/test`
- `cancel_order(...)` hits `DELETE /api/v3/order`
- `get_open_orders(symbol)` hits `GET /api/v3/openOrders`
- `get_order_status(symbol, client_order_id)` hits `GET /api/v3/order`

Sketch:

```python
"""ROB-298 — Spot Demo execution backend.

Mutation-capable adapter for ``demo-api.binance.com``. Enforces:

* ``BINANCE_SPOT_DEMO_*`` env namespace only.
* Host allowlist: ``demo-api.binance.com``.
* Default-disabled: ``BINANCE_SPOT_DEMO_ENABLED=true`` required.
* Per-call operator gate: ``submit_order(..., confirm=True)`` required
  to dispatch a real order. Default is ``preview_submit(...)`` returning
  ``SpotDemoDryRunResult`` with zero HTTP.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.services.brokers.binance.spot_demo.dto import (
    SpotDemoCancelResult,
    SpotDemoOpenOrder,
    SpotDemoOpenOrdersResult,
    SpotDemoOrderSubmitResult,
    SpotDemoOrderTestResult,
)
from app.services.brokers.binance.spot_demo.errors import (
    BinanceSpotDemoDisabled,
    BinanceSpotDemoMissingCredentials,
)
from app.services.brokers.binance.spot_demo.host_allowlist import (
    assert_spot_demo_host,
)
from app.services.brokers.binance.spot_demo.transport import (
    SpotDemoTransport,
)


_DEFAULT_BASE_URL = "https://demo-api.binance.com"


@dataclass(frozen=True)
class SpotDemoDryRunResult:
    symbol: str
    side: str
    order_type: str
    qty: Decimal
    client_order_id: str
    reason: str = "no confirm=True passed; HTTP not dispatched"


class BinanceSpotDemoExecutionClient:
    def __init__(self, *, transport: SpotDemoTransport) -> None:
        self._transport = transport

    @classmethod
    def from_env(cls) -> "BinanceSpotDemoExecutionClient":
        if os.environ.get("BINANCE_SPOT_DEMO_ENABLED", "false").lower() != "true":
            raise BinanceSpotDemoDisabled(
                "BINANCE_SPOT_DEMO_ENABLED is not 'true'"
            )
        api_key = os.environ.get("BINANCE_SPOT_DEMO_API_KEY") or ""
        api_secret = os.environ.get("BINANCE_SPOT_DEMO_API_SECRET") or ""
        if not api_key or not api_secret:
            raise BinanceSpotDemoMissingCredentials(
                "BINANCE_SPOT_DEMO_API_KEY and BINANCE_SPOT_DEMO_API_SECRET must be set"
            )
        base_url = os.environ.get("BINANCE_SPOT_DEMO_BASE_URL", _DEFAULT_BASE_URL)
        assert_spot_demo_host(base_url)
        transport = SpotDemoTransport(
            base_url=base_url, api_key=api_key, api_secret=api_secret
        )
        return cls(transport=transport)

    def preview_submit(
        self,
        *,
        symbol: str,
        side: str,
        order_type: str,
        qty: Decimal,
        client_order_id: str,
    ) -> SpotDemoDryRunResult:
        return SpotDemoDryRunResult(
            symbol=symbol,
            side=side,
            order_type=order_type,
            qty=qty,
            client_order_id=client_order_id,
        )

    async def submit_order(
        self,
        *,
        symbol: str,
        side: str,
        order_type: str,
        qty: Decimal,
        client_order_id: str,
        confirm: bool,
        price: Decimal | None = None,
        time_in_force: str | None = None,
    ) -> SpotDemoOrderSubmitResult | SpotDemoDryRunResult:
        if not confirm:
            return self.preview_submit(
                symbol=symbol, side=side, order_type=order_type,
                qty=qty, client_order_id=client_order_id,
            )
        params: dict[str, Any] = {
            "symbol": symbol,
            "side": side,
            "type": order_type,
            "quantity": format(qty, "f"),
            "newClientOrderId": client_order_id,
        }
        if order_type == "LIMIT":
            assert price is not None and time_in_force is not None
            params["price"] = format(price, "f")
            params["timeInForce"] = time_in_force
        resp = await self._transport.signed_post("/api/v3/order", params)
        return SpotDemoOrderSubmitResult(
            client_order_id=resp.get("clientOrderId", client_order_id),
            broker_order_id=str(resp.get("orderId", "")),
            symbol=resp.get("symbol", symbol),
            side=resp.get("side", side),
            order_type=resp.get("type", order_type),
            qty=Decimal(str(resp.get("origQty", qty))),
            executed_qty=Decimal(str(resp.get("executedQty", "0"))),
            cummulative_quote_qty=Decimal(str(resp.get("cummulativeQuoteQty", "0"))),
            status=resp.get("status", "UNKNOWN"),
            raw_response_redacted=_redact(resp),
        )

    async def order_test(
        self,
        *,
        symbol: str,
        side: str,
        order_type: str,
        qty: Decimal,
        price: Decimal | None = None,
        time_in_force: str | None = None,
    ) -> SpotDemoOrderTestResult:
        params: dict[str, Any] = {
            "symbol": symbol,
            "side": side,
            "type": order_type,
            "quantity": format(qty, "f"),
        }
        if order_type == "LIMIT":
            assert price is not None and time_in_force is not None
            params["price"] = format(price, "f")
            params["timeInForce"] = time_in_force
        await self._transport.signed_post("/api/v3/order/test", params)
        return SpotDemoOrderTestResult(
            symbol=symbol, side=side, order_type=order_type, qty=qty
        )

    async def cancel_order(
        self, *, symbol: str, client_order_id: str
    ) -> SpotDemoCancelResult:
        resp = await self._transport.signed_delete(
            "/api/v3/order",
            {"symbol": symbol, "origClientOrderId": client_order_id},
        )
        return SpotDemoCancelResult(
            client_order_id=resp.get("clientOrderId", client_order_id),
            broker_order_id=str(resp.get("orderId", "")),
            symbol=resp.get("symbol", symbol),
            status=resp.get("status", "CANCELED"),
            raw_response_redacted=_redact(resp),
        )

    async def get_open_orders(self, *, symbol: str) -> SpotDemoOpenOrdersResult:
        resp = await self._transport.signed_get(
            "/api/v3/openOrders", {"symbol": symbol}
        )
        orders = [
            SpotDemoOpenOrder(
                client_order_id=o.get("clientOrderId", ""),
                broker_order_id=str(o.get("orderId", "")),
                symbol=o.get("symbol", symbol),
                side=o.get("side", ""),
                qty=Decimal(str(o.get("origQty", "0"))),
                status=o.get("status", ""),
            )
            for o in resp
        ]
        return SpotDemoOpenOrdersResult(orders=orders)


def _redact(d: Any) -> dict[str, Any]:
    """Remove keys that could carry secrets; keep order metadata."""
    if not isinstance(d, dict):
        return {"_raw": "<non-dict response>"}
    redacted = {}
    for k, v in d.items():
        if k.lower() in {"apikey", "api_key", "secret", "signature"}:
            redacted[k] = "***REDACTED***"
        else:
            redacted[k] = v
    return redacted
```

**Note:** If `SpotDemoTransport` does not yet expose `signed_post`, `signed_delete`, `signed_get`, read the existing `transport.py` and add the missing methods following the existing `signed_get` pattern. Each method must call `assert_spot_demo_host(self._base_url)` before issuing the request. If they already exist with different names (e.g. `request_signed`), adjust the call sites in `execution_client.py` to match.

- [ ] **Step 5: Update `spot_demo/__init__.py`**

Open `app/services/brokers/binance/spot_demo/__init__.py` and add:

```python
from app.services.brokers.binance.spot_demo.execution_client import (
    BinanceSpotDemoExecutionClient,
    SpotDemoDryRunResult,
)
```

Remove any `BinanceSpotDemoOrderSubmitNotImplemented` re-export. Update `__all__` if present.

- [ ] **Step 6: Run all tests, verify pass**

```bash
uv run pytest tests/services/brokers/binance/spot_demo/ -v
```
Expected: all PASS (new + existing preflight/transport tests).

- [ ] **Step 7: Commit**

```bash
git add app/services/brokers/binance/spot_demo/execution_client.py \
        app/services/brokers/binance/spot_demo/errors.py \
        app/services/brokers/binance/spot_demo/__init__.py \
        tests/services/brokers/binance/spot_demo/test_execution_client_*.py
git commit -m "feat(rob-298): Spot Demo execution client (submit/test/cancel/status)"
```

---

## Task 9: Cross-environment leakage test — TESTNET_* env does not activate Demo

**Files:**
- Create: `tests/services/brokers/binance/spot_demo/test_testnet_env_does_not_activate_demo.py`

This is a new test file (not the existing `test_cross_environment_leakage.py`, which covers host allowlists). This one specifically asserts that setting `BINANCE_TESTNET_*` does not satisfy any precondition of the Spot Demo execution client.

- [ ] **Step 1: Write the test**

```python
"""ROB-298 — BINANCE_TESTNET_* must not activate Spot Demo trading."""
from __future__ import annotations

import pytest

from app.services.brokers.binance.spot_demo.errors import (
    BinanceSpotDemoDisabled,
    BinanceSpotDemoMissingCredentials,
)
from app.services.brokers.binance.spot_demo.execution_client import (
    BinanceSpotDemoExecutionClient,
)


def test_only_testnet_env_does_not_enable_demo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BINANCE_SPOT_DEMO_ENABLED", raising=False)
    monkeypatch.delenv("BINANCE_SPOT_DEMO_API_KEY", raising=False)
    monkeypatch.delenv("BINANCE_SPOT_DEMO_API_SECRET", raising=False)
    monkeypatch.setenv("BINANCE_TESTNET_ENABLED", "true")
    monkeypatch.setenv("BINANCE_TESTNET_API_KEY", "testnet-key")
    monkeypatch.setenv("BINANCE_TESTNET_API_SECRET", "testnet-secret")
    with pytest.raises(BinanceSpotDemoDisabled):
        BinanceSpotDemoExecutionClient.from_env()


def test_testnet_creds_do_not_substitute_for_demo_creds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BINANCE_SPOT_DEMO_ENABLED", "true")
    monkeypatch.delenv("BINANCE_SPOT_DEMO_API_KEY", raising=False)
    monkeypatch.delenv("BINANCE_SPOT_DEMO_API_SECRET", raising=False)
    monkeypatch.setenv("BINANCE_TESTNET_API_KEY", "testnet-key")
    monkeypatch.setenv("BINANCE_TESTNET_API_SECRET", "testnet-secret")
    with pytest.raises(BinanceSpotDemoMissingCredentials):
        BinanceSpotDemoExecutionClient.from_env()
```

- [ ] **Step 2: Run, verify pass**

```bash
uv run pytest tests/services/brokers/binance/spot_demo/test_testnet_env_does_not_activate_demo.py -v
```
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/services/brokers/binance/spot_demo/test_testnet_env_does_not_activate_demo.py
git commit -m "test(rob-298): BINANCE_TESTNET_* does not activate Spot Demo trading"
```

---

## Task 10: Spot Demo smoke CLI — full 5-mode

**Files:**
- Modify: `scripts/binance_spot_demo_smoke.py`

Current CLI supports `--plan-only`, `--preflight`, and `--dry-run` (default). Add `--order-test` and `--confirm` (the latter being the operator gate that allows real Demo order submission, with `--symbol BTCUSDT` and `--cap-usdt 10` defaults).

- [ ] **Step 1: Read current CLI shape**

```bash
uv run python scripts/binance_spot_demo_smoke.py --help
```
Note the existing argparse structure.

- [ ] **Step 2: Rewrite the CLI**

The CLI's responsibilities:

1. **default-disabled**: if `BINANCE_SPOT_DEMO_ENABLED != "true"`, print `disabled — set BINANCE_SPOT_DEMO_ENABLED=true to opt in`, exit 0.
2. **--plan-only**: print a JSON plan (no HTTP) showing the order it *would* preview. Exit 0.
3. **--preflight**: signed `GET /api/v3/account`, print redacted balance summary, exit 0.
4. **--order-test**: signed `POST /api/v3/order/test` for the configured symbol/qty, print redacted response, exit 0.
5. **--confirm**: signed `POST /api/v3/order` (real Demo order), then either `--side BUY` followed by a `--close-with SELL|CANCEL` step, with reconciliation read at the end. Print redacted evidence with `client_order_id`s and `broker_order_id`s. Exit 0 if reconciled clean.

Use `compute_demo_order_qty` (Task 7) for sizing; pull `MIN_NOTIONAL` / `stepSize` from a one-shot public `GET /api/v3/exchangeInfo?symbol=BTCUSDT` (no signing required for that read).

Wire the ledger: instantiate `BinanceDemoLedgerService` and record each lifecycle state (planned → previewed → validated → submitted → filled → closed → reconciled). For each transition print a one-line evidence row.

Argparse skeleton:

```python
parser = argparse.ArgumentParser(description="ROB-298 Binance Spot Demo smoke")
mode = parser.add_mutually_exclusive_group(required=False)
mode.add_argument("--plan-only", action="store_true")
mode.add_argument("--preflight", action="store_true")
mode.add_argument("--order-test", action="store_true")
mode.add_argument("--confirm", action="store_true",
                  help="Operator gate: dispatch real Demo orders. ROB-298 authorizes Demo only.")
parser.add_argument("--symbol", default="BTCUSDT")
parser.add_argument("--side", choices=["BUY", "SELL"], default="BUY")
parser.add_argument("--cap-usdt", type=Decimal, default=Decimal("10"))
parser.add_argument(
    "--close-with",
    choices=["SELL", "CANCEL"],
    default="SELL",
    help="How to close the position after a confirmed BUY (SELL=market sell; CANCEL only valid for LIMIT)",
)
```

- [ ] **Step 3: Smoke-run `--help`**

```bash
uv run python scripts/binance_spot_demo_smoke.py --help
```
Expected: clean help text with all 5 modes visible.

- [ ] **Step 4: Run plan-only (no env required)**

```bash
uv run python scripts/binance_spot_demo_smoke.py --plan-only --symbol BTCUSDT
```
Expected: JSON plan printed, exit 0, no HTTP.

- [ ] **Step 5: Run with BINANCE_SPOT_DEMO_ENABLED=false (must exit 0 disabled)**

```bash
BINANCE_SPOT_DEMO_ENABLED=false uv run python scripts/binance_spot_demo_smoke.py --preflight
```
Expected: prints `disabled`, exit 0.

- [ ] **Step 6: Commit**

```bash
git add scripts/binance_spot_demo_smoke.py
git commit -m "feat(rob-298): Spot Demo smoke CLI — 5 modes incl. --order-test and --confirm"
```

---

## Task 11: Static import guard — prevent testnet re-introduction

**Files:**
- Create: `tests/services/brokers/binance/demo/test_no_testnet_imports.py`

This is a structural test that fails the suite if anyone re-introduces an import from the deleted testnet runtime. Must be added BEFORE Task 12 (the deletion) so the failure first proves the deletion is needed, then after deletion proves it stays gone.

- [ ] **Step 1: Write the test**

```python
"""ROB-298 — Static import guard.

No production code in ``app/`` may import from
``app.services.brokers.binance.testnet`` or ``app.services.scalping``
(both deleted in ROB-298 PR 1). Tests under ``tests/`` may not either
(no stale dead imports).

Scripts under ``scripts/`` are checked separately.
"""
from __future__ import annotations

import ast
import pathlib

_BANNED_PREFIXES = (
    "app.services.brokers.binance.testnet",
    "app.services.scalping",
)


def _scan(roots: list[pathlib.Path]) -> list[str]:
    offenders: list[str] = []
    for root in roots:
        for py in root.rglob("*.py"):
            try:
                tree = ast.parse(py.read_text())
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    if node.module and any(
                        node.module.startswith(p) for p in _BANNED_PREFIXES
                    ):
                        offenders.append(f"{py}: from {node.module} import ...")
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        if any(alias.name.startswith(p) for p in _BANNED_PREFIXES):
                            offenders.append(f"{py}: import {alias.name}")
    return offenders


def test_no_testnet_imports_in_app() -> None:
    offenders = _scan([pathlib.Path("app")])
    assert not offenders, (
        "ROB-298 forbids imports from "
        f"{_BANNED_PREFIXES}. Offenders:\n" + "\n".join(offenders)
    )


def test_no_testnet_imports_in_scripts() -> None:
    offenders = _scan([pathlib.Path("scripts")])
    assert not offenders, (
        "ROB-298 forbids imports from "
        f"{_BANNED_PREFIXES}. Offenders:\n" + "\n".join(offenders)
    )


def test_no_testnet_imports_in_tests() -> None:
    offenders = _scan([pathlib.Path("tests")])
    assert not offenders, (
        "ROB-298 forbids imports from "
        f"{_BANNED_PREFIXES}. Offenders:\n" + "\n".join(offenders)
    )
```

- [ ] **Step 2: Run, expect FAIL (testnet still exists)**

```bash
uv run pytest tests/services/brokers/binance/demo/test_no_testnet_imports.py -v
```
Expected: FAIL with a long list of offending files (the testnet runtime, scalping runtime, scripts, and tests).

This is the proof that Task 12 is needed. Do not commit this test yet — commit it after Task 12 when it passes.

---

## Task 12: Delete testnet runtime + scalping/ directory

**Files:**
- Delete: `app/services/brokers/binance/testnet/` (entire directory)
- Delete: `app/services/scalping/` (entire directory)
- Delete: `app/models/binance_testnet_order_ledger.py`

- [ ] **Step 1: Verify nothing critical still uses these (one last grep)**

```bash
grep -rn "binance.testnet" app/ --include="*.py" | grep -v "app/services/brokers/binance/testnet/" || echo "no app/ leftover"
grep -rn "services.scalping" app/ --include="*.py" | grep -v "app/services/scalping/" || echo "no app/ leftover"
```
Expected: both prints `no app/ leftover`. If anything else is listed, it must be edited or deleted before the next step.

- [ ] **Step 2: Delete the directories and the model**

```bash
git rm -r app/services/brokers/binance/testnet/
git rm -r app/services/scalping/
git rm app/models/binance_testnet_order_ledger.py
```

- [ ] **Step 3: Remove the import from `app/models/__init__.py`**

Open `app/models/__init__.py` and remove the line `from app.models.binance_testnet_order_ledger import BinanceTestnetOrderLedger` (and the corresponding `__all__` entry if present).

- [ ] **Step 4: Run the model load smoke**

```bash
uv run python -c "import app.models; print('ok')"
```
Expected: `ok` (no ImportError).

- [ ] **Step 5: Commit**

```bash
git add app/models/__init__.py
git commit -m "feat(rob-298): delete Binance testnet runtime + scalping/ + ORM model"
```

---

## Task 13: Delete testnet CLI scripts and tests

**Files:**
- Delete: `scripts/binance_testnet_lifecycle_smoke.py`
- Delete: `scripts/binance_testnet_scalper_smoke.py`
- Delete: `scripts/binance_testnet_seed_instruments.py`
- Delete: `tests/services/brokers/binance/testnet/` (entire directory)
- Delete: `tests/services/scalping/` (entire directory)

- [ ] **Step 1: Delete files**

```bash
git rm scripts/binance_testnet_lifecycle_smoke.py
git rm scripts/binance_testnet_scalper_smoke.py
git rm scripts/binance_testnet_seed_instruments.py
git rm -r tests/services/brokers/binance/testnet/
git rm -r tests/services/scalping/
```

- [ ] **Step 2: Run the static import guard test (now should pass)**

```bash
uv run pytest tests/services/brokers/binance/demo/test_no_testnet_imports.py -v
```
Expected: 3 PASS.

- [ ] **Step 3: Commit static guard + deletions**

```bash
git add tests/services/brokers/binance/demo/test_no_testnet_imports.py
git commit -m "feat(rob-298): delete testnet scripts/tests + lock with AST guard"
```

---

## Task 14: Update `env.example`

**Files:**
- Modify: `env.example` (lines ~358-373 per ROB-296)

- [ ] **Step 1: Read current Binance block**

```bash
sed -n '355,380p' env.example
```

- [ ] **Step 2: Replace the block**

Remove all mention of `BINANCE_TESTNET_*` and `testnet.binance.vision` from comments. Re-anchor the section as canonical Demo. The replacement block:

```
# -----------------------------------------------------------------------------
# Binance Demo (canonical mock-trading backend, ROB-298)
# -----------------------------------------------------------------------------
# Spot Demo:     demo-api.binance.com   (BINANCE_SPOT_DEMO_*)
# Futures Demo:  demo-fapi.binance.com  (BINANCE_FUTURES_DEMO_*, added in PR 2)
#
# Live/mainnet hosts (api.binance.com, fapi.binance.com) are fail-closed at the
# transport layer; no env var can re-enable them. The legacy Binance Spot
# Testnet path (testnet.binance.vision) was removed in ROB-298. Setting any
# BINANCE_TESTNET_* variable here does nothing.
#
# Same credential may auth against both demo-api and demo-fapi, but each
# namespace must be set independently — namespaces are not aliased.

BINANCE_SPOT_DEMO_ENABLED=false
BINANCE_SPOT_DEMO_API_KEY=
BINANCE_SPOT_DEMO_API_SECRET=
BINANCE_SPOT_DEMO_BASE_URL=https://demo-api.binance.com
BINANCE_SPOT_DEMO_MAX_NOTIONAL_USDT=10
```

- [ ] **Step 3: Commit**

```bash
git add env.example
git commit -m "docs(rob-298): env.example — Demo canonical, testnet removed"
```

---

## Task 15: Delete testnet runbook, rewrite Spot Demo runbook

**Files:**
- Delete: `docs/runbooks/binance-testnet-scalping.md`
- Modify: `docs/runbooks/binance-spot-demo-smoke.md`

- [ ] **Step 1: Delete the testnet runbook**

```bash
git rm docs/runbooks/binance-testnet-scalping.md
```

- [ ] **Step 2: Rewrite the Spot Demo runbook**

The runbook should cover:

1. Lane boundaries (Spot Demo canonical; testnet removed; Futures Demo deferred to PR 2)
2. Env setup (`BINANCE_SPOT_DEMO_*`)
3. Pre-conditions and safety guarantees
4. 5 modes (default-disabled / plan-only / preflight / order-test / confirmed-demo-order)
5. Confirmed smoke runbook: BTCUSDT, 10 USDT cap, expected redacted evidence shape
6. Reconciliation checklist (no stale open orders after a smoke)
7. Linked rollback: if a smoke leaves state, manual `cancel_order` + `get_open_orders` + ledger `record_cancelled` / `record_anomaly`

Use the existing skeleton from `docs/runbooks/binance-spot-demo-smoke.md` (ROB-296) — extend with the new content rather than rewriting from scratch. Add references to the ROB-298 comment `d258c471-3202-444b-901b-c127f3ee44af` for the locked decisions.

- [ ] **Step 3: Commit**

```bash
git add docs/runbooks/binance-spot-demo-smoke.md
git commit -m "docs(rob-298): Spot Demo runbook — full lifecycle, testnet runbook removed"
```

---

## Task 16: Update CLAUDE.md

**Files:**
- Modify: `CLAUDE.md`

The current `CLAUDE.md` has a section "Binance Testnet Order Ledger (ROB-286)" that documents the now-deleted artifacts. Replace with a Demo-oriented section.

- [ ] **Step 1: Find the section**

```bash
grep -n "Binance Testnet Order Ledger" CLAUDE.md
```

- [ ] **Step 2: Replace the entire `### Binance Testnet Order Ledger (ROB-286)` section**

Delete the whole subsection from `### Binance Testnet Order Ledger (ROB-286)` down to (but not including) the next `### ` heading. Insert in its place:

```markdown
### Binance Demo Order Ledger (ROB-298)

`binance_demo_order_ledger` — unified Demo execution lifecycle ledger. Keyed by `product` discriminator (`spot` in PR 1; `usdm_futures` reserved for PR 2). All writes via service layer.

- **ORM 모델**: `app/models/binance_demo_order_ledger.BinanceDemoOrderLedger`
- **서비스**: `app/services/brokers/binance/demo/ledger/service.BinanceDemoLedgerService` — 모든 쓰기는 이 서비스를 통해서만 (8개 `record_*` 메서드)
- **리포지토리**: `app/services/brokers/binance/demo/ledger/repository.BinanceDemoLedgerRepository` — 서비스 내부 전용 (AST guard로 외부 import 금지)
- **상태 머신**: `BinanceDemoInvalidStateTransition` — `planned → previewed → validated → submitted → filled → closed → reconciled` + `cancelled`/`anomaly` branches
- **Spot 실행 어댑터**: `app/services/brokers/binance/spot_demo/execution_client.BinanceSpotDemoExecutionClient` — `demo-api.binance.com` only; mutation은 `submit_order(..., confirm=True)` 만
- **CLI**: `scripts/binance_spot_demo_smoke.py` (default-disabled, 5 modes)
- **런북**: `docs/runbooks/binance-spot-demo-smoke.md`

**안전 경계**:
- **Demo 전용 호스트**: Spot Demo는 `demo-api.binance.com`만 허용 (`assert_spot_demo_host`); live/mainnet/testnet host는 transport 레이어에서 fail-closed
- **Default-disabled**: `BINANCE_SPOT_DEMO_ENABLED=true` 미설정 시 `BinanceSpotDemoDisabled`
- **Per-call operator gate**: `submit_order(..., confirm=True)` 매 호출마다 명시되어야 실 HTTP 발생; default는 `SpotDemoDryRunResult`
- **TESTNET env vars do nothing**: `BINANCE_TESTNET_*`는 Demo trading을 활성화 못함 (테스트로 증명)
- **Sizing**: LOT_SIZE.stepSize floor, MIN_NOTIONAL guard, round-up 금지 — cap 초과면 blocked
- **선물 path**: PR 2에서 별도 `futures_demo/` backend로 추가; 이 PR에는 없음
- **스케줄러 활성화 없음**: TaskIQ/cron/Prefect 연결 없음. CLI에서만 호출
- **프로덕션 cutover gate**: alembic 마이그레이션은 PR에 포함되지만 operator가 별도로 `alembic upgrade head` 실행
```

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md
git commit -m "docs(rob-298): CLAUDE.md — Demo canonical, testnet section removed"
```

---

## Task 17: Full local test sweep + lint

- [ ] **Step 1: Run all tests**

```bash
uv run pytest tests/ -v --tb=short 2>&1 | tail -60
```
Expected: all green. Investigate any FAIL — most likely culprits are tests that imported testnet symbols. Fix or delete.

- [ ] **Step 2: Run ruff**

```bash
uv run ruff check app/ scripts/ tests/
```
Expected: clean.

- [ ] **Step 3: Run type check**

```bash
make typecheck 2>&1 | tail -20
```
Expected: clean (or pre-existing baseline; don't introduce new errors).

- [ ] **Step 4: If anything broke, fix and commit before proceeding**

```bash
git add -A
git commit -m "chore(rob-298): post-deletion test/lint fixes"
```

---

## Task 18: Confirmed Demo smoke run (operator-gated)

This task requires real `BINANCE_SPOT_DEMO_API_KEY` / `_API_SECRET` in the environment. If they are not available, mark this task BLOCKED and continue to Task 19. Do NOT skip silently — report the block in the PR handoff.

- [ ] **Step 1: Plan-only sanity**

```bash
uv run python scripts/binance_spot_demo_smoke.py --plan-only --symbol BTCUSDT
```
Expected: JSON plan printed, exit 0, no HTTP.

- [ ] **Step 2: Preflight**

```bash
BINANCE_SPOT_DEMO_ENABLED=true uv run python scripts/binance_spot_demo_smoke.py --preflight
```
Expected: signed `GET /api/v3/account` succeeds; balances printed with key fields redacted; exit 0.

Capture the output to `/tmp/rob-298-preflight-evidence.txt`.

- [ ] **Step 3: Order-test**

```bash
BINANCE_SPOT_DEMO_ENABLED=true uv run python scripts/binance_spot_demo_smoke.py --order-test --symbol BTCUSDT --cap-usdt 10
```
Expected: signed `POST /api/v3/order/test` succeeds with empty body; redacted log printed; exit 0; no real order.

Capture output to `/tmp/rob-298-ordertest-evidence.txt`.

- [ ] **Step 4: Confirmed tiny BUY + SELL**

```bash
BINANCE_SPOT_DEMO_ENABLED=true uv run python scripts/binance_spot_demo_smoke.py \
    --confirm --symbol BTCUSDT --cap-usdt 10 --side BUY --close-with SELL
```
Expected sequence in CLI output:
1. `planned` row written (client_order_id printed)
2. `previewed` row
3. `validated` row
4. `submitted` row (broker_order_id printed)
5. `filled` row
6. `closed` row (sell completed)
7. `reconciled` row (open orders empty + position flat)
Exit 0.

Capture output to `/tmp/rob-298-confirmed-evidence.txt`.

- [ ] **Step 5: Verify reconciliation in DB**

```bash
docker compose exec postgres psql -U postgres -d auto_trader -c \
  "SELECT client_order_id, lifecycle_state, side, qty, broker_order_id, notional_usdt FROM binance_demo_order_ledger ORDER BY created_at DESC LIMIT 4;"
```
Expected: 2 rows (BUY + SELL), both `lifecycle_state='reconciled'`, broker_order_id populated, notional ≤ 10.

- [ ] **Step 6: Verify no stale open orders on Demo side**

```bash
BINANCE_SPOT_DEMO_ENABLED=true uv run python -c "
import asyncio
from app.services.brokers.binance.spot_demo.execution_client import BinanceSpotDemoExecutionClient
async def main():
    c = BinanceSpotDemoExecutionClient.from_env()
    r = await c.get_open_orders(symbol='BTCUSDT')
    print('open orders:', r.orders)
asyncio.run(main())
"
```
Expected: `open orders: []`.

If anything stale is reported, manually cancel via `--confirm --close-with CANCEL` or directly call `cancel_order(...)` and capture redacted evidence; record an `anomaly` row with reason in the ledger. Do NOT mark the smoke successful until reconciled clean.

---

## Task 19: Push branch, create PR

- [ ] **Step 1: Final status**

```bash
git status
git log --oneline origin/main..HEAD
```
Expected: clean working tree, ~10-15 commits ahead of `main`.

- [ ] **Step 2: Push**

```bash
git push -u origin rob-298
```

- [ ] **Step 3: Create PR**

Use `gh pr create` with the PR body template below. PR title: `feat(rob-298): Binance Spot Demo canonicalization + testnet removal (PR 1 of 2)`.

```bash
gh pr create --base main --title "feat(rob-298): Binance Spot Demo canonicalization + testnet removal (PR 1 of 2)" --body "$(cat <<'EOF'
## Summary

Implements PR 1 of ROB-298: makes Binance Spot Demo the canonical spot mock-trading backend with mutation-capable order execution, introduces the unified `binance_demo_order_ledger`, and physically removes the obsolete Binance Spot Testnet active trading path. PR 2 (USD-M Futures Demo) follows.

Locked design decisions for this issue: ROB-298 comment `d258c471-3202-444b-901b-c127f3ee44af`.

## Scope

- ✅ Drop `binance_testnet_order_ledger` table via forward alembic migration; create `binance_demo_order_ledger` keyed by `product` discriminator (`spot` | `usdm_futures`)
- ✅ `BinanceDemoLedgerService` with state machine (planned → previewed → validated → submitted → filled → closed → reconciled, plus cancelled/anomaly)
- ✅ `BinanceSpotDemoExecutionClient` with `preview_submit`, `submit_order(..., confirm=True)`, `order_test`, `cancel_order`, `get_open_orders`, `get_order_status`
- ✅ Sizing helper: LOT_SIZE floor + MIN_NOTIONAL guard, never round up past 10 USDT cap
- ✅ `scripts/binance_spot_demo_smoke.py` now supports 5 modes: `default-disabled`, `--plan-only`, `--preflight`, `--order-test`, `--confirm`
- ✅ Static AST import guard: nothing in `app/`, `scripts/`, or `tests/` imports from the deleted testnet runtime or `app.services.scalping`
- ✅ Cross-environment leakage test: `BINANCE_TESTNET_*` cannot activate Spot Demo
- ✅ Deleted: `app/services/brokers/binance/testnet/`, `app/services/scalping/`, `app/models/binance_testnet_order_ledger.py`, all testnet scripts, all testnet/scalping tests, `docs/runbooks/binance-testnet-scalping.md`
- ✅ Updated: `env.example`, `docs/runbooks/binance-spot-demo-smoke.md`, `CLAUDE.md`

## Out of scope (deferred)

- USD-M Futures Demo backend (`futures_demo/`) — PR 2 of ROB-298
- TaskIQ/Prefect/scheduler activation — ROB-292 follow-up
- Hermes/Discord integration — separate
- Live/mainnet hosts — fail-closed structurally

## Migrations

- `<new_rev>_replace_testnet_with_demo_ledger.py`: drops `binance_testnet_order_ledger`, creates `binance_demo_order_ledger`. **No data preservation** (operator approval given for clean cut; testnet was never in production use).
- Operator must run `alembic upgrade head` post-merge.

## Tests run

- `uv run pytest tests/services/brokers/binance/spot_demo/ -v` → all PASS
- `uv run pytest tests/services/brokers/binance/demo/ -v` → all PASS
- `uv run pytest tests/ -v` → all PASS (full sweep)
- `uv run ruff check app/ scripts/ tests/` → clean
- `make typecheck` → clean

## Demo smoke results

(Fill in from `/tmp/rob-298-*-evidence.txt` files. If smoke was BLOCKED due to missing creds, state so explicitly here with the failure mode.)

- `--plan-only`: ✅ no HTTP, JSON plan emitted
- `--preflight`: ✅ signed GET /api/v3/account → 200, balances printed redacted
- `--order-test`: ✅ signed POST /api/v3/order/test → 200 (empty), no real order
- `--confirm` (BUY + SELL, BTCUSDT, 10 USDT cap): ✅ 7-state lifecycle round-trip, ledger reconciled, post-run `get_open_orders` empty

## Safety boundary verification

- ✅ `BINANCE_SPOT_DEMO_ENABLED=false` → `BinanceSpotDemoDisabled` raised before any HTTP
- ✅ `BINANCE_SPOT_DEMO_BASE_URL=https://api.binance.com` → `BinanceSpotDemoCrossAllowlistViolation` raised at construction
- ✅ `BINANCE_TESTNET_*` set without `BINANCE_SPOT_DEMO_*` → `BinanceSpotDemoDisabled`/`MissingCredentials`
- ✅ Static AST guard: 0 imports of `app.services.brokers.binance.testnet` or `app.services.scalping`

## Test plan

- [ ] `uv run alembic upgrade head` on a clean staging DB
- [ ] Operator manually runs `--preflight` against Demo creds and confirms balance redaction
- [ ] Operator runs `--order-test` and confirms no order on Demo account
- [ ] (Optional) Operator runs `--confirm` smoke and confirms reconciled
- [ ] PR 2 (Futures Demo) will follow within the same ROB-298 Linear issue

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 4: Capture PR URL and update Linear**

After PR creation, capture the URL. Post a comment to ROB-298:

```
PR 1 created: <URL>

Outstanding: confirmed Demo smoke evidence in PR body (fill in from /tmp/rob-298-*-evidence.txt or mark BLOCKED if creds unavailable). PR 2 (Futures Demo) to follow in same Linear issue.
```

Use `mcp__plugin_linear_linear__save_comment` with `issueId="ROB-298"` and the body above.

---

## Self-Review Checklist

After completing all tasks, the reviewer (you, with fresh eyes) verifies:

1. **Acceptance criteria from ROB-298** — walk through each `- [ ]` in the issue body:
   - [x] Active Binance scalping code no longer depends on Spot Testnet — Tasks 11–13
   - [x] Spot Demo mutation-capable backend with `demo-api.binance.com` allowlist — Task 8
   - [ ] Futures Demo mutation-capable backend — **deferred to PR 2**
   - [x] Actual Demo buy/sell smoke with small caps + explicit flags — Tasks 7, 10, 18
   - [x] Spot Demo order-test path covered — Task 10
   - [ ] Futures Demo order-test path — **deferred to PR 2**
   - [x] Confirmed Demo smoke ends without stale state — Task 18 step 5, 6
   - [x] Tests prove live/mainnet hosts fail-closed — Task 8 fail-closed tests
   - [x] Tests prove `BINANCE_TESTNET_*` does not activate active trading — Task 9
   - [x] Docs describe Demo as canonical, testnet removed — Tasks 14–16
   - [x] PR handoff with branch, URL, migrations, tests, smoke results — Task 19

2. **Locked decisions** — match ROB-298 comment d258c471:
   - PR split: PR 1 covers Spot canonicalization + testnet removal + demo ledger ✅
   - Ledger: unified `binance_demo_order_ledger` with product dim ✅
   - Env: `BINANCE_SPOT_DEMO_*` only (no shared namespace) ✅
   - Spot Demo defaults: BTCUSDT, 10 USDT cap, 5 modes ✅
   - Sizing rule: LOT_SIZE floor, no round-up ✅
   - Host: `demo-api.binance.com` only ✅
   - ROB-291/ROB-292: not touched ✅

3. **No placeholders** — scan for: "TODO", "TBD", "implement later", "similar to Task N", "add appropriate error handling". None present.

4. **Type/name consistency** — `BinanceDemoLedgerService` used identically across Tasks 5, 10, 16, 19. `record_*` method names match between service definition (Task 5) and CLI usage (Task 10).
