# ROB-653 P6-B — Shared `_place_order_impl` approval-hash + KIS intent guard — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bind previewed→placed orders with a content approval-hash and add double-send protection to the shared kis_live/crypto order path, mirroring the merged Toss (ROB-651 P6-A) contract.

**Architecture:** Reuse P6-A's pure approval primitives (`toss_approval.py`) via a thin generic module; insert a hash guard into `_place_order_impl` between preview and execute (dry_run=True emits the token, dry_run=False verifies under a rollout-mode gate); add a KIS-only pre-send reservation table for local idempotency (KIS has no broker key) and promote Upbit's `identifier` to a content key.

**Tech Stack:** Python 3.13, SQLAlchemy async, Alembic, pytest, FastMCP tooling.

## Global Constraints

- Python 3.13+; async SQLAlchemy; all migrations chain from a single alembic head.
- **New migration `down_revision = "20260702_rob651"`** (current head after rebase).
- Ledger schema changes are **additive nullable only**; reconcile (ROB-395/407) must stay unchanged.
- All DB writes go through service/helper layers — **no raw INSERT/UPDATE/DELETE** in tools.
- Ship rollout gate at `optional` (no behavior change for existing callers).
- Commit message trailer: `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.
- Run `make format && make lint` before each commit; CI lint checks `app/ tests/` with ruff format.

---

### Task 1: Shared generic approval helper module

**Files:**
- Create: `app/mcp_server/tooling/order_approval.py`
- Test: `tests/test_order_approval.py`

**Interfaces:**
- Consumes: `app.mcp_server.tooling.toss_approval` primitives (`build`/verify/digest/client-id).
- Produces:
  - `build_order_canonical_payload(*, market_type: str, symbol: str, side: str, order_type: str, quantity: str | None, price: str | None) -> dict[str, Any]`
  - `salt_market_for(market_type: str) -> str` → `"us"` for `equity_us`, else `"kr"`.
  - Re-exports: `encode_approval_token`, `verify_approval_token`, `derive_approval_digest`, `derive_client_order_id`, `APPROVAL_TTL_SECONDS`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_order_approval.py
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.mcp_server.tooling import order_approval as oa

KST = ZoneInfo("Asia/Seoul")


def _canon(**over):
    base = dict(
        market_type="equity_kr", symbol="005930", side="buy",
        order_type="limit", quantity="10", price="70000",
    )
    base.update(over)
    return oa.build_order_canonical_payload(**base)


def test_canonical_is_deterministic_and_side_upcased():
    c = _canon()
    assert c["side"] == "BUY"
    assert c["orderType"] == "LIMIT"
    assert c["market_type"] == "equity_kr"
    assert c == _canon()  # stable


def test_salt_market_maps_us_else_kr():
    assert oa.salt_market_for("equity_us") == "us"
    assert oa.salt_market_for("equity_kr") == "kr"
    assert oa.salt_market_for("crypto") == "kr"


def test_token_roundtrip_and_mismatch_diff():
    now = datetime(2026, 7, 2, 10, 0, tzinfo=KST)
    canonical = _canon()
    token = oa.encode_approval_token(canonical, now=now)
    ok = oa.verify_approval_token(token, canonical, now=now)
    assert ok.ok is True and ok.digest.startswith("p6a-")

    changed = _canon(quantity="11")
    bad = oa.verify_approval_token(token, changed, now=now)
    assert bad.ok is False and bad.error_code == "approval_hash_mismatch"
    assert bad.diff["quantity"] == {"previewed": "10", "placing": "11"}


def test_idempotency_key_same_day_stable_next_day_differs():
    canonical = _canon()
    d1 = datetime(2026, 7, 2, 10, 0, tzinfo=KST)
    d1b = datetime(2026, 7, 2, 15, 0, tzinfo=KST)
    d2 = datetime(2026, 7, 3, 10, 0, tzinfo=KST)
    k1 = oa.derive_client_order_id(canonical, market="kr", now=d1)
    k1b = oa.derive_client_order_id(canonical, market="kr", now=d1b)
    k2 = oa.derive_client_order_id(canonical, market="kr", now=d2)
    assert k1 == k1b
    assert k1 != k2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_order_approval.py -v`
Expected: FAIL — `ModuleNotFoundError: app.mcp_server.tooling.order_approval`.

- [ ] **Step 3: Write minimal implementation**

```python
# app/mcp_server/tooling/order_approval.py
"""ROB-653 P6-B — generic order approval-hash helpers for the shared
_place_order_impl path (kis_live KR/US + upbit crypto).

Reuses the pure P6-A primitives verbatim (app.mcp_server.tooling.toss_approval);
only the canonical payload builder is broker-generic. The shared P6 token
version/digest prefix are intentionally reused: the generic canonical key set
differs structurally from Toss's, so a token minted on one path fails the
canonical-equality check on the other (fail-closed, non-interchangeable).
"""

from __future__ import annotations

from typing import Any

from app.mcp_server.tooling.toss_approval import (
    APPROVAL_TTL_SECONDS,
    derive_approval_digest,
    derive_client_order_id,
    encode_approval_token,
    verify_approval_token,
)

__all__ = [
    "APPROVAL_TTL_SECONDS",
    "build_order_canonical_payload",
    "salt_market_for",
    "derive_approval_digest",
    "derive_client_order_id",
    "encode_approval_token",
    "verify_approval_token",
]


def build_order_canonical_payload(
    *,
    market_type: str,
    symbol: str,
    side: str,
    order_type: str,
    quantity: str | None,
    price: str | None,
) -> dict[str, Any]:
    """Canonical order content shared by the dry-run preview and the live send.

    ``quantity``/``price`` must already be stringified post-normalization wire
    values (tick-snapped price, amount→quantity resolved) or ``None`` so preview
    and place derive an identical digest.
    """
    return {
        "market_type": market_type,
        "symbol": symbol,
        "side": side.upper(),
        "orderType": order_type.upper(),
        "quantity": quantity,
        "price": price,
    }


def salt_market_for(market_type: str) -> str:
    """Trading-day salt market: US equities settle on ET, everything else KST."""
    return "us" if market_type == "equity_us" else "kr"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_order_approval.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
make format
git add app/mcp_server/tooling/order_approval.py tests/test_order_approval.py
git commit -m "feat(ROB-653): generic order approval-hash helper (reuses P6-A primitives)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: Config + env rollout gate

**Files:**
- Modify: `app/core/config.py` (Settings class, near `toss_approval_hash_mode`)
- Modify: `env.example` (near `TOSS_APPROVAL_HASH_MODE`)
- Test: `tests/test_order_approval_config.py`

**Interfaces:**
- Produces: `settings.order_approval_hash_mode: str` (default `"optional"`).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_order_approval_config.py
from app.core.config import settings


def test_order_approval_hash_mode_defaults_optional():
    assert settings.order_approval_hash_mode == "optional"
    assert settings.order_approval_hash_mode in {"off", "optional", "warn", "required"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_order_approval_config.py -v`
Expected: FAIL — `AttributeError: 'Settings' object has no attribute 'order_approval_hash_mode'`.

- [ ] **Step 3: Write minimal implementation**

In `app/core/config.py`, immediately after the `toss_approval_hash_mode` line:

```python
    # ROB-653 P6-B — kis_live/crypto place-order approval-hash enforcement level.
    # off | optional | warn | required. optional = no behavior change.
    order_approval_hash_mode: str = "optional"
```

In `env.example`, after the `TOSS_APPROVAL_HASH_MODE` line:

```bash
# ROB-653 P6-B — kis_live/crypto preview→place approval-hash 강제 수준 (off|optional|warn|required)
ORDER_APPROVAL_HASH_MODE=optional
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_order_approval_config.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/core/config.py env.example tests/test_order_approval_config.py
git commit -m "feat(ROB-653): order_approval_hash_mode config gate (default optional)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: Migration + models (ledger columns + order_send_intents)

**Files:**
- Create: `alembic/versions/20260702_rob653_order_approval_intent.py`
- Modify: `app/models/review.py` (add columns to `KISLiveOrderLedger`, `LiveOrderLedger`; add `OrderSendIntent`)
- Test: `tests/test_rob653_order_intent_schema.py`

**Interfaces:**
- Produces:
  - `KISLiveOrderLedger.approval_hash: str | None`, `.idempotency_key: str | None`
  - `LiveOrderLedger.approval_hash: str | None`, `.idempotency_key: str | None`
  - `OrderSendIntent` model → table `review.order_send_intents` with columns
    `id, account_scope, idempotency_key, symbol, side, created_at`;
    `UNIQUE(account_scope, idempotency_key)` named `uq_order_send_intent_scope_key`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_rob653_order_intent_schema.py
from app.models.review import KISLiveOrderLedger, LiveOrderLedger, OrderSendIntent


def test_ledger_additive_columns_present():
    for model in (KISLiveOrderLedger, LiveOrderLedger):
        cols = model.__table__.columns
        assert "approval_hash" in cols
        assert "idempotency_key" in cols
        assert cols["approval_hash"].nullable
        assert cols["idempotency_key"].nullable


def test_order_send_intent_table_and_unique():
    t = OrderSendIntent.__table__
    assert t.name == "order_send_intents"
    assert t.schema == "review"
    names = {c.name for c in t.columns}
    assert {"id", "account_scope", "idempotency_key", "symbol", "side", "created_at"} <= names
    uniques = {tuple(sorted(c.name for c in con.columns))
               for con in t.constraints
               if con.__class__.__name__ == "UniqueConstraint"}
    assert ("account_scope", "idempotency_key") in uniques
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_rob653_order_intent_schema.py -v`
Expected: FAIL — `ImportError: cannot import name 'OrderSendIntent'`.

- [ ] **Step 3a: Add model columns + new model in `app/models/review.py`**

In `class KISLiveOrderLedger`, after the `report_item_uuid` column:

```python
    # ROB-653 P6-B — content approval-hash + local idempotency key (additive).
    approval_hash: Mapped[str | None] = mapped_column(Text)
    idempotency_key: Mapped[str | None] = mapped_column(Text)
```

In `class LiveOrderLedger`, after its `report_item_uuid` column:

```python
    # ROB-653 P6-B — content approval-hash + local idempotency key (additive).
    approval_hash: Mapped[str | None] = mapped_column(Text)
    idempotency_key: Mapped[str | None] = mapped_column(Text)
```

Add a new model (near the other review-schema ledgers; reuse existing imports
`Base, Mapped, mapped_column, BigInteger, Text, TIMESTAMP, UniqueConstraint, func, datetime`):

```python
class OrderSendIntent(Base):
    """ROB-653 P6-B — KIS pre-send reservation for local double-send protection.

    KIS has no broker idempotency field, so a UNIQUE (account_scope,
    idempotency_key) row is inserted immediately before the order POST. A
    same-key insert the same trading day raises IntegrityError → fail-closed.
    Never read by reconcile; purely a send-time guard.
    """

    __tablename__ = "order_send_intents"
    __table_args__ = (
        UniqueConstraint(
            "account_scope", "idempotency_key",
            name="uq_order_send_intent_scope_key",
        ),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    account_scope: Mapped[str] = mapped_column(Text, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)
    symbol: Mapped[str | None] = mapped_column(Text)
    side: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
```

(If `UniqueConstraint`/`func`/`TIMESTAMP` are not already imported at the top of
`review.py`, add them to the existing sqlalchemy import block.)

- [ ] **Step 3b: Write the migration**

```python
# alembic/versions/20260702_rob653_order_approval_intent.py
"""ROB-653 P6-B — order ledger approval_hash/idempotency_key + order_send_intents

Revision ID: 20260702_rob653
Revises: 20260702_rob651
Create Date: 2026-07-02

Additive: nullable approval_hash/idempotency_key on kis_live_order_ledger and
live_order_ledger, plus review.order_send_intents (KIS pre-send reservation with
UNIQUE(account_scope, idempotency_key)). No changes to reconcile behavior.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260702_rob653"
down_revision: str | Sequence[str] | None = "20260702_rob651"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LEDGERS = ("kis_live_order_ledger", "live_order_ledger")


def upgrade() -> None:
    for table in _LEDGERS:
        op.add_column(table, sa.Column("approval_hash", sa.Text(), nullable=True), schema="review")
        op.add_column(table, sa.Column("idempotency_key", sa.Text(), nullable=True), schema="review")

    op.create_table(
        "order_send_intents",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("account_scope", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=True),
        sa.Column("side", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "account_scope", "idempotency_key",
            name="uq_order_send_intent_scope_key",
        ),
        schema="review",
    )


def downgrade() -> None:
    op.drop_table("order_send_intents", schema="review")
    for table in _LEDGERS:
        op.drop_column(table, "idempotency_key", schema="review")
        op.drop_column(table, "approval_hash", schema="review")
```

- [ ] **Step 4: Run tests + verify single alembic head**

Run: `uv run pytest tests/test_rob653_order_intent_schema.py -v`
Expected: PASS (2 tests).
Run: `uv run alembic heads`
Expected: a single head `20260702_rob653`.

- [ ] **Step 5: Commit**

```bash
make format
git add app/models/review.py alembic/versions/20260702_rob653_order_approval_intent.py tests/test_rob653_order_intent_schema.py
git commit -m "feat(ROB-653): migration — ledger approval_hash/idempotency_key + order_send_intents

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: OrderSendIntentService.reserve + DuplicateOrderIntent

**Files:**
- Create: `app/services/order_send_intent_service.py`
- Test: `tests/services/test_order_send_intent_service.py`

**Interfaces:**
- Consumes: `OrderSendIntent` model (Task 3); an async session factory.
- Produces:
  - `class DuplicateOrderIntent(Exception)`
  - `class OrderSendIntentService:` with
    `async def reserve(self, *, account_scope: str, idempotency_key: str, symbol: str | None = None, side: str | None = None) -> int` (returns row id; raises `DuplicateOrderIntent` on conflict).

- [ ] **Step 1: Write the failing test**

```python
# tests/services/test_order_send_intent_service.py
import pytest

from app.models.review import OrderSendIntent  # noqa: F401 (ensures table registered)
from app.services.order_send_intent_service import (
    DuplicateOrderIntent,
    OrderSendIntentService,
)


@pytest.mark.asyncio
async def test_reserve_inserts_then_blocks_duplicate(async_session):
    svc = OrderSendIntentService(async_session)
    rid = await svc.reserve(
        account_scope="kis_live", idempotency_key="p6a-abc", symbol="005930", side="buy"
    )
    assert isinstance(rid, int)

    with pytest.raises(DuplicateOrderIntent):
        await svc.reserve(
            account_scope="kis_live", idempotency_key="p6a-abc", symbol="005930", side="buy"
        )


@pytest.mark.asyncio
async def test_reserve_allows_distinct_key(async_session):
    svc = OrderSendIntentService(async_session)
    await svc.reserve(account_scope="kis_live", idempotency_key="p6a-day1")
    # a different key (e.g. next trading-day salt) is allowed
    rid = await svc.reserve(account_scope="kis_live", idempotency_key="p6a-day2")
    assert isinstance(rid, int)
```

> Note: `async_session` is the existing project fixture yielding an `AsyncSession`
> against the test DB. If the repo's fixture name differs, use the established one
> from `tests/conftest.py` (grep `async_session` / `db_session`).

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/services/test_order_send_intent_service.py -v`
Expected: FAIL — `ModuleNotFoundError: app.services.order_send_intent_service`.

- [ ] **Step 3: Write minimal implementation**

```python
# app/services/order_send_intent_service.py
"""ROB-653 P6-B — KIS pre-send reservation service.

Writes the sole double-send guard for KIS live orders (no broker idempotency
key). All writes go through this service — no raw SQL. Never read by reconcile.
"""

from __future__ import annotations

import logging

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.review import OrderSendIntent

logger = logging.getLogger(__name__)


class DuplicateOrderIntent(Exception):
    """Raised when (account_scope, idempotency_key) is already reserved."""


class OrderSendIntentService:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def reserve(
        self,
        *,
        account_scope: str,
        idempotency_key: str,
        symbol: str | None = None,
        side: str | None = None,
    ) -> int:
        row = OrderSendIntent(
            account_scope=account_scope,
            idempotency_key=idempotency_key,
            symbol=symbol,
            side=side,
        )
        self._db.add(row)
        try:
            await self._db.flush()
        except IntegrityError as exc:
            await self._db.rollback()
            raise DuplicateOrderIntent(
                f"order intent already reserved: {account_scope}/{idempotency_key}"
            ) from exc
        rid = row.id
        await self._db.commit()
        return rid
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/services/test_order_send_intent_service.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
make format
git add app/services/order_send_intent_service.py tests/services/test_order_send_intent_service.py
git commit -m "feat(ROB-653): OrderSendIntentService.reserve (fail-closed on duplicate)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5: Ledger record pass-through (approval_hash/idempotency_key)

**Files:**
- Modify: `app/mcp_server/tooling/kis_live_ledger.py` (`_save_kis_live_order_ledger` insert values + `_record_kis_live_order` signature/call)
- Modify: `app/mcp_server/tooling/live_order_ledger.py` (`_record_live_order` signature + `LiveOrderLedger(...)` row)
- Test: `tests/test_rob653_ledger_passthrough.py`

**Interfaces:**
- Consumes: ledger columns from Task 3.
- Produces: both record functions accept `approval_hash: str | None = None`,
  `idempotency_key: str | None = None` and persist them.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_rob653_ledger_passthrough.py
import pytest
from sqlalchemy import select

from app.models.review import KISLiveOrderLedger, LiveOrderLedger
from app.mcp_server.tooling.kis_live_ledger import _record_kis_live_order
from app.mcp_server.tooling.live_order_ledger import _record_live_order


@pytest.mark.asyncio
async def test_kis_live_record_persists_hash_and_key(async_session):
    await _record_kis_live_order(
        normalized_symbol="005930", market_type="equity_kr", side="buy",
        order_type="limit",
        dry_run_result={"price": 70000, "quantity": 10, "estimated_value": 700000},
        execution_result={"odno": "KISTEST653KR", "rt_cd": "0", "msg": "ok"},
        reason="t", exit_reason=None, thesis="t", strategy="t",
        target_price=None, stop_loss=None, min_hold_days=None, notes=None,
        indicators_snapshot=None,
        approval_hash="p6a-deadbeef", idempotency_key="p6a-kr-key",
    )
    row = (await async_session.execute(
        select(KISLiveOrderLedger).where(KISLiveOrderLedger.order_no == "KISTEST653KR")
    )).scalar_one()
    assert row.approval_hash == "p6a-deadbeef"
    assert row.idempotency_key == "p6a-kr-key"


@pytest.mark.asyncio
async def test_live_order_record_persists_hash_and_key(async_session):
    await _record_live_order(
        broker="upbit", account_scope="upbit_live", market="crypto",
        normalized_symbol="BTC", exchange=None, market_symbol="KRW-BTC",
        side="buy", order_kind="limit", currency="KRW",
        order_no="UPBITTEST653", order_time=None, rt_cd="0", response_message=None,
        dry_run_result={"price": 1, "quantity": 1, "estimated_value": 1},
        execution_result={"uuid": "UPBITTEST653"},
        reason="t", exit_reason=None, thesis="t", strategy="t",
        target_price=None, stop_loss=None, min_hold_days=None, notes=None,
        indicators_snapshot=None,
        approval_hash="p6a-cafe", idempotency_key="p6a-crypto-key",
    )
    row = (await async_session.execute(
        select(LiveOrderLedger).where(LiveOrderLedger.order_no == "UPBITTEST653")
    )).scalar_one()
    assert row.approval_hash == "p6a-cafe"
    assert row.idempotency_key == "p6a-crypto-key"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_rob653_ledger_passthrough.py -v`
Expected: FAIL — `TypeError: ... unexpected keyword argument 'approval_hash'`.

- [ ] **Step 3a: Thread through `kis_live_ledger.py`**

Add params to `_save_kis_live_order_ledger` (the `pg_insert` helper): add
`approval_hash: str | None = None, idempotency_key: str | None = None,` to its
signature, and add to the `.values(...)` block after `report_item_uuid=report_item_uuid,`:

```python
                    approval_hash=approval_hash,
                    idempotency_key=idempotency_key,
```

Add the same two params to `_record_kis_live_order`'s signature (after
`report_item_uuid: uuid.UUID | None = None,`) and forward them in the
`_save_kis_live_order_ledger(...)` call after `report_item_uuid=report_item_uuid,`:

```python
        approval_hash=approval_hash,
        idempotency_key=idempotency_key,
```

- [ ] **Step 3b: Thread through `live_order_ledger.py`**

Add `approval_hash: str | None = None, idempotency_key: str | None = None,` to
`_record_live_order`'s signature (after `report_item_uuid: uuid.UUID | None = None,`),
and add to the `LiveOrderLedger(...)` constructor after `report_item_uuid=report_item_uuid,`:

```python
            approval_hash=approval_hash,
            idempotency_key=idempotency_key,
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_rob653_ledger_passthrough.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
make format
git add app/mcp_server/tooling/kis_live_ledger.py app/mcp_server/tooling/live_order_ledger.py tests/test_rob653_ledger_passthrough.py
git commit -m "feat(ROB-653): persist approval_hash/idempotency_key on kis_live + live ledgers

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 6: Hash guard in `_place_order_impl` + param threading

**Files:**
- Modify: `app/mcp_server/tooling/order_execution.py` (`_place_order_impl`: new params, guard block, dry-run token emission; `_execute_and_record`: new params + forward to ledger records)
- Modify: `app/mcp_server/tooling/orders_kis_variants.py` (`kis_live_place_order` + `_place_order_variant`: `approval_hash`/`rung` params)
- Modify: `app/mcp_server/tooling/orders_registration.py` (`place_order`: `approval_hash`/`rung` params + forward)
- Test: `tests/test_rob653_place_order_hash_guard.py`

**Interfaces:**
- Consumes: `order_approval` (Task 1), `settings.order_approval_hash_mode` (Task 2), ledger record params (Task 5).
- Produces:
  - `_place_order_impl(..., approval_hash: str | None = None, rung: str | int | None = None)`
  - dry-run response gains `approval_hash`, `approval_expires_at`, `idempotency_key`.
  - `_execute_and_record(..., approval_hash_digest: str | None = None, idempotency_key: str | None = None)` forwarding both to `_record_kis_live_order` / `_record_live_order`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_rob653_place_order_hash_guard.py
import pytest

import app.mcp_server.tooling.order_execution as oe
from app.core.config import settings


@pytest.fixture
def _stub_pricing(monkeypatch):
    # Keep the flow offline: fixed price, no balance block, no real send.
    async def _price(symbol, market_type):
        return 70000.0
    monkeypatch.setattr(oe, "_get_current_price_for_order", _price)


@pytest.mark.asyncio
async def test_dry_run_emits_approval_hash(monkeypatch, _stub_pricing):
    res = await oe._place_order_impl(
        symbol="005930", side="buy", market="KR", order_type="limit",
        quantity=10, price=70000, dry_run=True, thesis="t", strategy="t",
    )
    assert res["success"] is True and res["dry_run"] is True
    assert res["approval_hash"].startswith("p6a1.")
    assert "approval_expires_at" in res
    assert res["idempotency_key"].startswith("p6a-")


@pytest.mark.asyncio
async def test_required_mode_blocks_without_hash(monkeypatch, _stub_pricing):
    monkeypatch.setattr(settings, "order_approval_hash_mode", "required")
    res = await oe._place_order_impl(
        symbol="005930", side="buy", market="KR", order_type="limit",
        quantity=10, price=70000, dry_run=False, thesis="t", strategy="t",
    )
    assert res["success"] is False
    assert res["error_code"] == "approval_hash_required"


@pytest.mark.asyncio
async def test_mismatched_hash_fails_closed_with_diff(monkeypatch, _stub_pricing):
    monkeypatch.setattr(settings, "order_approval_hash_mode", "required")
    preview = await oe._place_order_impl(
        symbol="005930", side="buy", market="KR", order_type="limit",
        quantity=10, price=70000, dry_run=True, thesis="t", strategy="t",
    )
    token = preview["approval_hash"]
    # place a DIFFERENT quantity with the old token
    res = await oe._place_order_impl(
        symbol="005930", side="buy", market="KR", order_type="limit",
        quantity=11, price=70000, dry_run=False, thesis="t", strategy="t",
        approval_hash=token,
    )
    assert res["success"] is False
    assert res["error_code"] == "approval_hash_mismatch"
    assert "diff" in res
```

> The guard runs before any send; `test_required_mode_blocks_without_hash` and the
> mismatch test return at the guard, so no broker call happens. Keep `dry_run=True`
> assertions independent of DB.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_rob653_place_order_hash_guard.py -v`
Expected: FAIL — `approval_hash` not in dry-run result / unexpected kwarg `approval_hash`.

- [ ] **Step 3a: Add params + guard in `_place_order_impl`**

Add to the `_place_order_impl` signature (after `report_item_uuid: str | None = None,`):

```python
    approval_hash: str | None = None,
    rung: str | int | None = None,
```

Add imports near the top of `order_execution.py`:

```python
from datetime import timedelta

from app.core.timezone import KST, now_kst
from app.mcp_server.tooling import order_approval
```

Replace the block that currently reads (around L1161–1215):

```python
        order_amount = _to_float(dry_run_result.get("estimated_value"), default=0.0)
```

…keep that line, then **after** the balance pre-check / before the `if dry_run:` exit,
insert the canonical + guard. Concretely, change the region from `# Dry-run exit`
through the `_execute_and_record(` call to:

```python
        # ROB-653 P6-B — bind previewed↔placed content with an approval hash.
        # Canonical uses post-normalization wire values (tick-snap, amount→qty).
        canonical = order_approval.build_order_canonical_payload(
            market_type=market_type,
            symbol=normalized_symbol,
            side=side_lower,
            order_type=order_type_lower,
            quantity=None if order_quantity is None else str(order_quantity),
            price=None if price is None else str(price),
        )
        now = now_kst()
        salt_market = order_approval.salt_market_for(market_type)
        idempotency_key = order_approval.derive_client_order_id(
            canonical, market=salt_market, now=now, rung=rung
        )

        # Dry-run exit — the preview emits the approval token operators pass back.
        if dry_run:
            preview_resp = _build_dry_run_response(dry_run_result, balance_warning)
            preview_resp["approval_hash"] = order_approval.encode_approval_token(
                canonical, now=now
            )
            preview_resp["approval_expires_at"] = (
                (now + timedelta(seconds=order_approval.APPROVAL_TTL_SECONDS))
                .astimezone(KST)
                .isoformat()
            )
            preview_resp["idempotency_key"] = idempotency_key
            return preview_resp

        # Live send — approval-hash gate (valid hash = confirm).
        mode = getattr(settings, "order_approval_hash_mode", "optional")
        if mode != "off":
            if approval_hash is not None:
                verdict = order_approval.verify_approval_token(
                    approval_hash, canonical, now=now
                )
                if not verdict.ok:
                    err = _order_error(verdict.message or "approval_hash invalid")
                    err["error_code"] = verdict.error_code
                    if verdict.diff is not None:
                        err["diff"] = verdict.diff
                    return err
            elif mode == "required":
                err = _order_error(
                    "approval_hash is required (ORDER_APPROVAL_HASH_MODE=required). "
                    "Re-run with dry_run=True and pass the returned approval_hash."
                )
                err["error_code"] = "approval_hash_required"
                return err
            elif mode == "warn":
                logger.warning(
                    "place_order without approval_hash (mode=warn) symbol=%s side=%s",
                    normalized_symbol,
                    side_lower,
                )

        approval_digest = (
            order_approval.derive_approval_digest(canonical) if mode != "off" else None
        )

        # Real execution
        return await _execute_and_record(
            normalized_symbol=normalized_symbol,
            side=side_lower,
            order_type=order_type_lower,
            order_quantity=order_quantity,
            price=price,
            market_type=market_type,
            current_price=current_price,
            avg_price=avg_price,
            dry_run_result=dry_run_result,
            order_amount=order_amount,
            reason=reason,
            exit_reason=exit_reason,
            thesis=thesis,
            strategy=strategy,
            target_price=target_price,
            stop_loss=stop_loss,
            min_hold_days=min_hold_days,
            notes=notes,
            indicators_snapshot=indicators_snapshot,
            defensive_trim_ctx=defensive_trim_ctx,
            order_error_fn=_order_error,
            is_mock=is_mock,
            correlation_id=correlation_id,
            report_item_uuid=_coerce_report_item_uuid(report_item_uuid),
            approval_hash_digest=approval_digest,
            idempotency_key=idempotency_key,
        )
```

> Preserve the existing `balance_error` handling that sits before the dry-run exit —
> only the `if dry_run:` block and the `_execute_and_record` call change.

- [ ] **Step 3b: Thread params through `_execute_and_record`**

Add to `_execute_and_record`'s signature (after `report_item_uuid: uuid.UUID | None = None,`):

```python
    approval_hash_digest: str | None = None,
    idempotency_key: str | None = None,
```

Forward them in the `_record_kis_live_order(...)` call (add after `report_item_uuid=report_item_uuid,`):

```python
            approval_hash=approval_hash_digest,
            idempotency_key=idempotency_key,
```

Forward them in **both** `_record_live_order(...)` calls (US + crypto branches; add
after `report_item_uuid=report_item_uuid,`):

```python
            approval_hash=approval_hash_digest,
            idempotency_key=idempotency_key,
```

- [ ] **Step 3c: Thread `approval_hash`/`rung` through the wrappers**

In `orders_kis_variants.py`: add `approval_hash: str | None = None,` and
`rung: str | int | None = None,` to `kis_live_place_order`'s signature and to
`_place_order_variant`'s signature; forward them in the `_place_order_impl(...)` call
inside `_place_order_variant` (after `report_item_uuid=report_item_uuid,`):

```python
            approval_hash=approval_hash,
            rung=rung,
```

and in `kis_live_place_order`'s call to `_place_order_variant(...)`:

```python
            approval_hash=approval_hash,
            rung=rung,
```

In `orders_registration.py`: add `approval_hash: str | None = None,` and
`rung: str | int | None = None,` to `place_order`'s signature and forward them in the
`order_execution._place_order_impl(...)` call (the live branch, after
`report_item_uuid=report_item_uuid,`):

```python
                approval_hash=approval_hash,
                rung=rung,
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_rob653_place_order_hash_guard.py -v`
Expected: PASS (3 tests).
Run: `uv run pytest tests/test_mcp_toss_order_variants.py tests/test_order_approval.py -v`
Expected: PASS (no regression in the Toss path).

- [ ] **Step 5: Commit**

```bash
make format && make lint
git add app/mcp_server/tooling/order_execution.py app/mcp_server/tooling/orders_kis_variants.py app/mcp_server/tooling/orders_registration.py tests/test_rob653_place_order_hash_guard.py
git commit -m "feat(ROB-653): approval-hash guard in _place_order_impl (dry_run emits, live verifies)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 7: KIS pre-send intent reservation wiring

**Files:**
- Modify: `app/mcp_server/tooling/order_execution.py` (`_execute_and_record`: reserve before send for KIS markets)
- Test: `tests/test_rob653_kis_intent_guard.py`

**Interfaces:**
- Consumes: `OrderSendIntentService`/`DuplicateOrderIntent` (Task 4), `idempotency_key` param in `_execute_and_record` (Task 6).
- Produces: for `market_type in {"equity_kr", "equity_us"}` and not `is_mock`, a reservation
  is inserted immediately before `_execute_order`; duplicate → `order_error_fn(...)` returned
  (no send). Uses `account_scope="kis_live"`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_rob653_kis_intent_guard.py
import pytest

import app.mcp_server.tooling.order_execution as oe
from app.services.order_send_intent_service import OrderSendIntentService


@pytest.mark.asyncio
async def test_kis_duplicate_intent_blocks_second_send(monkeypatch, async_session):
    # Pre-reserve the key so the guard sees a conflict on the real path.
    sent = {"count": 0}

    async def _fake_execute(**kwargs):
        sent["count"] += 1
        return {"odno": "KISDUP653", "rt_cd": "0", "msg": "ok"}

    monkeypatch.setattr(oe, "_execute_order", _fake_execute)

    key = "p6a-kr-dup"
    svc = OrderSendIntentService(async_session)
    await svc.reserve(account_scope="kis_live", idempotency_key=key)

    err = await oe._execute_and_record(
        normalized_symbol="005930", side="buy", order_type="limit",
        order_quantity=10, price=70000, market_type="equity_kr",
        current_price=70000, avg_price=0.0,
        dry_run_result={"price": 70000, "quantity": 10, "estimated_value": 700000},
        order_amount=700000, reason="t", exit_reason=None, thesis="t", strategy="t",
        target_price=None, stop_loss=None, min_hold_days=None, notes=None,
        indicators_snapshot=None, defensive_trim_ctx=None,
        order_error_fn=lambda m: {"success": False, "error": m},
        idempotency_key=key,
    )
    assert err["success"] is False
    assert "intent" in err["error"].lower() or "중복" in err["error"]
    assert sent["count"] == 0  # never sent
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_rob653_kis_intent_guard.py -v`
Expected: FAIL — send happens (`sent["count"] == 1`) / no guard.

- [ ] **Step 3: Wire the reservation in `_execute_and_record`**

Add an import near the top of `order_execution.py`:

```python
from app.services.order_send_intent_service import (
    DuplicateOrderIntent,
    OrderSendIntentService,
)
```

In `_execute_and_record`, immediately **before** the `try:` that calls `_execute_order`
(after the `kis_mock_baseline_qty` block), insert:

```python
    # ROB-653 P6-B — KIS has no broker idempotency key; reserve a local intent
    # row before the send. A same-key send the same trading day fails closed.
    # Crypto/Upbit is excluded (it uses the broker-side content identifier).
    if (
        not is_mock
        and idempotency_key is not None
        and market_type in ("equity_kr", "equity_us")
    ):
        async with _order_session_factory()() as intent_db:
            try:
                await OrderSendIntentService(intent_db).reserve(
                    account_scope="kis_live",
                    idempotency_key=idempotency_key,
                    symbol=normalized_symbol,
                    side=side,
                )
            except DuplicateOrderIntent:
                logger.warning(
                    "KIS duplicate order intent blocked: symbol=%s side=%s key=%s",
                    normalized_symbol,
                    side,
                    idempotency_key,
                )
                return order_error_fn(
                    "동일 주문이 오늘 이미 전송되어 중복 전송을 차단했습니다 "
                    "(duplicate order intent). 재전송하지 말고 reconcile로 접수 여부를 "
                    "확인하세요. 익일 재배치는 허용됩니다."
                )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_rob653_kis_intent_guard.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
make format
git add app/mcp_server/tooling/order_execution.py tests/test_rob653_kis_intent_guard.py
git commit -m "feat(ROB-653): KIS pre-send intent reservation (fail-closed double-send guard)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 8: Upbit content-based identifier

**Files:**
- Modify: `app/services/brokers/upbit/orders.py` (optional `identifier` on place fns)
- Modify: `app/mcp_server/tooling/order_execution.py` (`_execute_order`/`_execute_crypto_order` pass `idempotency_key`)
- Test: `tests/test_rob653_upbit_identifier.py`

**Interfaces:**
- Consumes: `idempotency_key` available in `_execute_and_record` (Task 6).
- Produces:
  - `place_buy_order(..., identifier: str | None = None)`, `place_sell_order(..., identifier: str | None = None)`, `place_market_buy_order(market, price, identifier=None)`, `place_market_sell_order(market, volume, identifier=None)` — use the passed identifier or fall back to uuid4.
  - `_execute_order(..., identifier: str | None = None)` and `_execute_crypto_order(..., identifier: str | None = None)` thread it to the upbit calls.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_rob653_upbit_identifier.py
import pytest

import app.services.brokers.upbit.orders as uorders


@pytest.mark.asyncio
async def test_place_buy_order_uses_supplied_identifier(monkeypatch):
    captured = {}

    async def _fake_request(method, url, body_params=None, query_params=None):
        captured["body"] = body_params
        return {"uuid": "x"}

    monkeypatch.setattr(uorders._client, "_request_with_auth", _fake_request)
    await uorders.place_buy_order("KRW-BTC", "1000", "0.5", "limit", identifier="p6a-content")
    assert captured["body"]["identifier"] == "p6a-content"


@pytest.mark.asyncio
async def test_place_buy_order_defaults_to_uuid_when_none(monkeypatch):
    captured = {}

    async def _fake_request(method, url, body_params=None, query_params=None):
        captured["body"] = body_params
        return {"uuid": "x"}

    monkeypatch.setattr(uorders._client, "_request_with_auth", _fake_request)
    await uorders.place_buy_order("KRW-BTC", "1000", "0.5", "limit")
    ident = captured["body"]["identifier"]
    assert ident and ident != "p6a-content"  # uuid4 fallback preserved
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_rob653_upbit_identifier.py -v`
Expected: FAIL — `place_buy_order() got an unexpected keyword argument 'identifier'`.

- [ ] **Step 3a: Add optional `identifier` in `upbit/orders.py`**

For each of `place_sell_order`, `place_market_sell_order`, `place_buy_order`,
`place_market_buy_order`: add `identifier: str | None = None` as the last param and
replace `"identifier": _new_order_identifier(),` with:

```python
        "identifier": identifier or _new_order_identifier(),
```

(Update `place_market_buy_order` similarly — grep it in the same file; it follows the
same body-dict pattern.)

- [ ] **Step 3b: Thread `identifier` from `_execute_order`**

In `order_execution.py`, add `identifier: str | None = None` to `_execute_order` and
`_execute_crypto_order` signatures. In `_execute_order`, pass `identifier=identifier`
to the `_execute_crypto_order(...)` call. In `_execute_crypto_order`, pass
`identifier=identifier` to each `upbit_service.place_*` call (buy limit, market buy,
market sell, sell limit).

In `_execute_and_record`, change the `_execute_order(...)` call to forward the key **only
for crypto** (KIS ignores it; KIS uses the reservation table instead):

```python
        execution_result = await _execute_order(
            symbol=normalized_symbol,
            side=side,
            order_type=order_type,
            quantity=order_quantity,
            price=price,
            market_type=market_type,
            is_mock=is_mock,
            identifier=idempotency_key if market_type == "crypto" else None,
        )
```

> `upbit_service` here refers to the module already imported in `order_execution.py`
> as `upbit_service`; its `place_*` functions re-export `app/services/brokers/upbit/orders.py`.
> Confirm the call sites in `_execute_crypto_order` accept the new kwarg.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_rob653_upbit_identifier.py -v`
Expected: PASS (2 tests).
Run: `uv run pytest tests/ -k "upbit_order or upbit and order" -q`
Expected: no regressions in existing Upbit order tests.

- [ ] **Step 5: Commit**

```bash
make format
git add app/services/brokers/upbit/orders.py app/mcp_server/tooling/order_execution.py tests/test_rob653_upbit_identifier.py
git commit -m "feat(ROB-653): content-based Upbit identifier (broker-side crypto dedupe)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 9: Docs (runbook + CLAUDE.md) + full-suite verification

**Files:**
- Create: `docs/runbooks/order-approval-hash.md`
- Modify: `CLAUDE.md` (new ROB-653 section)
- Modify: `docs/superpowers/plans/2026-07-02-rob-653-order-approval-hash-intent-guard.md` (check off completed tasks)

**Interfaces:** none (docs).

- [ ] **Step 1: Write the runbook**

Create `docs/runbooks/order-approval-hash.md` covering: what the guard does; the
`ORDER_APPROVAL_HASH_MODE` rollout stages (`off → optional → warn → required`) and how to
advance them; how an operator gets an `approval_hash` (run the order tool with
`dry_run=True`, copy `approval_hash`, pass it to the `dry_run=False` call within the
5-minute TTL); the KIS `order_send_intents` guard (same-day duplicate blocked, next-day
allowed; recovery = inspect `review.order_send_intents`, never delete rows mid-session);
and the explicit note that reconcile (ROB-395/407) is unchanged.

- [ ] **Step 2: Add the CLAUDE.md section**

Add a section mirroring the existing ROB-651/Toss entries:

```markdown
### Shared place-order approval-hash + KIS intent guard (ROB-653)

`kis_live_place_order` / generic `place_order`(crypto) now bind previewed→placed
content via an approval hash in the shared `_place_order_impl` seam (ROB-651 P6-A
primitives reused). `dry_run=True` emits `approval_hash` + `approval_expires_at`
(5-min TTL) + `idempotency_key`; `dry_run=False` verifies under
`ORDER_APPROVAL_HASH_MODE` (`off|optional|warn|required`, default `optional`) — a
valid hash doubles as confirm.

- **KIS pre-send guard**: `review.order_send_intents` (UNIQUE account_scope,
  idempotency_key) via `app/services/order_send_intent_service.OrderSendIntentService`
  — reserve-before-send, fail-closed on same-day duplicate (KIS has no broker
  idempotency key). Next trading day → new salt → allowed.
- **Upbit**: content-based `identifier` (broker-side dedupe).
- **Ledgers**: additive `approval_hash`/`idempotency_key` on `kis_live_order_ledger`
  + `live_order_ledger`. reconcile (ROB-395/407) unchanged.
- **런북**: `docs/runbooks/order-approval-hash.md`
```

- [ ] **Step 3: Run the full relevant suite + lint**

Run: `make format && make lint`
Expected: clean.
Run: `uv run pytest tests/test_order_approval.py tests/test_order_approval_config.py tests/test_rob653_order_intent_schema.py tests/services/test_order_send_intent_service.py tests/test_rob653_ledger_passthrough.py tests/test_rob653_place_order_hash_guard.py tests/test_rob653_kis_intent_guard.py tests/test_rob653_upbit_identifier.py -v`
Expected: all PASS.
Run: `uv run pytest tests/test_mcp_toss_order_variants.py tests/ -k "place_order or live_order_ledger or kis_live" -q`
Expected: no regressions (reconcile + existing order tests green).

- [ ] **Step 4: Commit**

```bash
git add docs/runbooks/order-approval-hash.md CLAUDE.md docs/superpowers/plans/2026-07-02-rob-653-order-approval-hash-intent-guard.md
git commit -m "docs(ROB-653): approval-hash runbook + CLAUDE.md section

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage:**
- 3.1 shared helper → Task 1 ✓
- 3.2 hash guard (dry_run-as-preview + modes) → Task 6 ✓
- 3.3 KIS intent guard table + service + wiring → Tasks 3, 4, 7 ✓
- 3.4 Upbit content identifier → Task 8 ✓
- 3.5 ledger additive columns → Tasks 3, 5 ✓
- 3.6 migration → Task 3 ✓
- 3.7 config/env → Task 2 ✓
- §5 tests → each task is TDD ✓; §6 docs → Task 9 ✓
- AC "reconcile unchanged" → Task 9 regression run + intent table never read ✓

**Type consistency:** `approval_hash`/`idempotency_key` used consistently across
ledger models (Task 3), record fns (Task 5), and `_execute_and_record` forwarding
(Task 6, param named `approval_hash_digest` for the ledger value = `derive_approval_digest`
output, distinct from the token param `approval_hash`). `idempotency_key` = client-order-id
from `derive_client_order_id`. `salt_market_for` / `build_order_canonical_payload` signatures
match Task 1 → Task 6 usage.

**Placeholder scan:** none — every code step shows concrete content.

## Notes / risks

- `_execute_and_record` opens a **separate session** for the reservation so the guard
  commit is independent of the later ledger write (which itself uses its own session).
  The reservation intentionally persists on send-timeout (`OrderSendOutcomeUnknown`) to
  keep the duplicate blocked until reconcile.
- If the repo's test session fixture is not named `async_session`, substitute the
  established fixture from `tests/conftest.py` in Tasks 4/5/7 (grep first).
- `place_market_buy_order` signature in `upbit/orders.py` must be grepped to confirm its
  exact params before editing (Task 8, Step 3a).
