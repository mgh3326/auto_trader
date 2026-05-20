# ROB-284 — Crypto Instruments Master + Venue-Aware Candle Schema — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> AOE_STATUS: plan_ready
> AOE_ISSUE: ROB-284
> AOE_ROLE: implementer
> AOE_NEXT: execute Task 1 (audit & branch setup), then proceed in order. Do not skip the audit task.

**Goal:** Introduce `crypto_instruments` as the master/source-of-truth for venue/product/symbol identity, migrate `crypto_candles_1d` in place to instrument-FK shape, and add a new `crypto_candles_1m` Timescale hypertable with the same shape. Confirm by audit that no other consumer of `crypto_candles_1d` exists, so ROB-282's screener UI/API contract is unaffected by construction.

**Architecture:** Add one new master table (`crypto_instruments`), add one new candle table (`crypto_candles_1m`), and in-place-migrate one existing table (`crypto_candles_1d`). Replace the existing raw-SQL-only data access for crypto candles with first-class SQLAlchemy models. All writes flow through `DailyCandlesRepository` / new `MinuteCandlesRepository` and reference instruments by `instrument_id` FK. `market` strings are removed from the candle row; identity is `(instrument_id, time)`.

**Tech Stack:** PostgreSQL 16 + TimescaleDB ≥2.15.0, SQLAlchemy 2 async + Alembic, pytest, `uv` for env/dependencies. No new third-party dependencies in this PR.

---

## Pre-implementation discovery (locked finding)

Audit performed on 2026-05-20 confirmed:

- `crypto_candles_1d` has exactly **one** reader/writer in `app/`: `app/services/daily_candles/repository.py`. Verified by `grep -rln "crypto_candles_1d" --include="*.py" app/`.
- The crypto screener snapshot builder reads from **TVScreener external API**, not from `crypto_candles_1d`. The chain is: TVScreener → `TvScreenerUpbitCryptoSnapshotProvider` → `build_crypto_snapshot_payloads` → `InvestCryptoScreenerSnapshotsRepository` → `invest_crypto_screener_snapshots` table → screener UI/API.

Therefore the parent plan's §4.9 "snapshot builder migration" task is a **no-op** and is dropped from this issue. The `crypto_candles_1d_view` is also dropped (YAGNI — no consumer today). Both decisions are reflected in ROB-284's description and a comment thread.

The audit is re-run as Task 1 of this plan to lock the invariant in the PR diff.

---

## Hard safety invariants (apply to all tasks in this PR)

1. **No broker/order/watch/order-intent mutation in this PR.** No new code in `app/services/brokers/`, no new ledger tables, no order-routing edits.
2. **No Binance SDK / WS / REST adapter.** That is Children B/C.
3. **No scheduler activation.** No new TaskIQ entries, no Prefect changes.
4. **No `crypto_candles_1d_view`.** YAGNI — there is no consumer today.
5. **No `app/jobs/invest_crypto_screener_snapshots.py` modification.** Audit confirms it does not touch candles.
6. **No production deployment of new tables in this PR.** Alembic upgrade runs locally and in test environments only.
7. **Backup before destructive migration.** The `crypto_candles_1d_pre_rob283` backup must exist before the in-place migration's destructive steps. Operator step in runbook, not in the migration itself.

---

## File structure

### Created
- `alembic/versions/<rev_a>_add_crypto_instruments.py` — create `crypto_instruments` table + indexes.
- `alembic/versions/<rev_b>_add_crypto_candles_1m.py` — create `crypto_candles_1m` table + hypertable + CHECKs.
- `alembic/versions/<rev_c>_migrate_crypto_candles_1d_to_instrument_fk.py` — three-stage migration of `crypto_candles_1d`.
- `app/models/crypto_instruments.py` — `CryptoInstrument` ORM model.
- `app/models/crypto_candles.py` — `CryptoCandle1d`, `CryptoCandle1m` ORM models (new file; previously raw-SQL-only).
- `app/services/minute_candles/__init__.py` and `app/services/minute_candles/repository.py` — `MinuteCandlesRepository`.
- `tests/services/daily_candles/test_crypto_instruments.py` — instrument table + UNIQUE + status CHECK.
- `tests/services/daily_candles/test_crypto_candles_1m.py` — schema, hypertable, idempotency, cross-venue coexistence.
- `tests/services/daily_candles/test_crypto_candles_1d_migration.py` — backfill correctness, post-migration shape.
- `tests/services/daily_candles/test_audit_no_other_consumer.py` — grep-based audit asserting no other reader.

### Modified
- `app/services/daily_candles/repository.py` — `MarketKey.CRYPTO` path writes via `instrument_id`; new `_resolve_instrument` helper.
- `docs/runbooks/daily-candles-store.md` — pre-migration backup procedure + rollback procedure.

### Not modified
- `app/jobs/invest_crypto_screener_snapshots.py` (audit-confirmed: no candle dependency).
- `app/services/invest_crypto_screener_snapshots/*` (audit-confirmed).
- `app/services/invest_view_model/*` (audit-confirmed).
- Any KR/US candle code (out of scope).

---

## Task list

### Task 1: Audit & branch setup

**Files:**
- Create: `tests/services/daily_candles/test_audit_no_other_consumer.py`

- [ ] **Step 1: Create the worktree per project convention**

```bash
cd ~/auto_trader && git switch main && git pull
git worktree add ~/auto_trader/.worktrees/ROB-284 -b feature/ROB-284-crypto-instruments-schema main
cd ~/auto_trader/.worktrees/ROB-284
```

- [ ] **Step 2: Run the audit grep and capture output**

```bash
grep -rln "crypto_candles_1d" --include="*.py" app/
```

Expected output (exactly one line):
```
app/services/daily_candles/repository.py
```

If any additional file appears, **stop and re-scope this PR** before proceeding — there is a previously-unknown consumer that must be migrated or escalated.

- [ ] **Step 3: Encode the audit as a failing test that pins the invariant**

Write `tests/services/daily_candles/test_audit_no_other_consumer.py`:

```python
"""Locks the invariant that crypto_candles_1d has exactly one reader/writer.

ROB-284 pre-implementation audit (2026-05-20): only
app/services/daily_candles/repository.py touches the table. If this test
fails after ROB-284 lands, a new consumer was added without migrating to
the new instrument-FK shape — re-evaluate before merging.
"""

from __future__ import annotations

import pathlib
import subprocess


ALLOWED = {"app/services/daily_candles/repository.py"}


def test_only_daily_candles_repository_references_crypto_candles_1d() -> None:
    repo_root = pathlib.Path(__file__).resolve().parents[3]
    result = subprocess.run(
        [
            "grep",
            "-rln",
            "crypto_candles_1d",
            "--include=*.py",
            "app/",
        ],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    files = {line.strip() for line in result.stdout.splitlines() if line.strip()}
    unexpected = files - ALLOWED
    assert not unexpected, (
        f"Unexpected files reference crypto_candles_1d: {sorted(unexpected)}. "
        "ROB-284 audit invariant violated."
    )
```

- [ ] **Step 4: Run test, expect PASS (audit is already true)**

```bash
uv run pytest tests/services/daily_candles/test_audit_no_other_consumer.py -v
```

Expected: 1 passed.

- [ ] **Step 5: Commit**

```bash
git add tests/services/daily_candles/test_audit_no_other_consumer.py
git commit -m "test(rob-284): lock audit invariant — crypto_candles_1d has single consumer"
```

---

### Task 2: `crypto_instruments` migration

**Files:**
- Create: `alembic/versions/<rev_a>_add_crypto_instruments.py`

- [ ] **Step 1: Write the failing schema test**

Create `tests/services/daily_candles/test_crypto_instruments.py`:

```python
"""ROB-284 — crypto_instruments table contract."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.asyncio
async def test_crypto_instruments_table_exists(session: AsyncSession) -> None:
    result = await session.execute(
        text(
            "SELECT column_name, is_nullable, data_type "
            "FROM information_schema.columns "
            "WHERE table_name = 'crypto_instruments' "
            "ORDER BY ordinal_position"
        )
    )
    cols = {row.column_name: (row.is_nullable, row.data_type) for row in result}
    assert "id" in cols
    assert "venue" in cols and cols["venue"][0] == "NO"
    assert "product" in cols and cols["product"][0] == "NO"
    assert "venue_symbol" in cols and cols["venue_symbol"][0] == "NO"
    assert "base_asset" in cols and cols["base_asset"][0] == "NO"
    assert "quote_asset" in cols and cols["quote_asset"][0] == "NO"
    assert "status" in cols and cols["status"][0] == "NO"
    for opt in ("precision_price", "precision_amount", "tick_size",
                "lot_size", "min_notional", "listed_at", "delisted_at", "metadata"):
        assert opt in cols, f"missing optional column {opt}"


@pytest.mark.asyncio
async def test_crypto_instruments_unique_constraint(session: AsyncSession) -> None:
    await session.execute(
        text(
            "INSERT INTO crypto_instruments "
            "(venue, product, venue_symbol, base_asset, quote_asset, status) "
            "VALUES ('upbit', 'spot', 'KRW-BTC', 'BTC', 'KRW', 'active')"
        )
    )
    await session.flush()
    with pytest.raises(IntegrityError):
        await session.execute(
            text(
                "INSERT INTO crypto_instruments "
                "(venue, product, venue_symbol, base_asset, quote_asset, status) "
                "VALUES ('upbit', 'spot', 'KRW-BTC', 'BTC', 'KRW', 'active')"
            )
        )
        await session.flush()


@pytest.mark.asyncio
async def test_crypto_instruments_status_check(session: AsyncSession) -> None:
    with pytest.raises(IntegrityError):
        await session.execute(
            text(
                "INSERT INTO crypto_instruments "
                "(venue, product, venue_symbol, base_asset, quote_asset, status) "
                "VALUES ('upbit', 'spot', 'KRW-XYZ', 'XYZ', 'KRW', 'bogus_status')"
            )
        )
        await session.flush()
```

- [ ] **Step 2: Run, expect FAIL**

```bash
uv run pytest tests/services/daily_candles/test_crypto_instruments.py -v
```

Expected: 3 failed (table does not exist).

- [ ] **Step 3: Write the migration**

```bash
uv run alembic revision -m "add crypto_instruments"
```

Edit the generated file (`alembic/versions/<rev_a>_add_crypto_instruments.py`):

```python
"""add crypto_instruments

Revision ID: <rev_a>
Revises: <prev_head>
Create Date: 2026-05-20

ROB-284 — master/source-of-truth table for venue/product/symbol identity.
"""
from alembic import op
import sqlalchemy as sa


revision = "<rev_a>"
down_revision = "<prev_head>"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "crypto_instruments",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("venue", sa.Text(), nullable=False),
        sa.Column("product", sa.Text(), nullable=False),
        sa.Column("venue_symbol", sa.Text(), nullable=False),
        sa.Column("base_asset", sa.Text(), nullable=False),
        sa.Column("quote_asset", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'active'"),
        ),
        sa.Column("precision_price", sa.Integer(), nullable=True),
        sa.Column("precision_amount", sa.Integer(), nullable=True),
        sa.Column("tick_size", sa.Numeric(), nullable=True),
        sa.Column("lot_size", sa.Numeric(), nullable=True),
        sa.Column("min_notional", sa.Numeric(), nullable=True),
        sa.Column("listed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("delisted_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("metadata", sa.dialects.postgresql.JSONB(), nullable=True),
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
        sa.UniqueConstraint(
            "venue", "product", "venue_symbol",
            name="uq_crypto_instruments_venue_product_symbol",
        ),
        sa.CheckConstraint(
            "status IN ('active','delisted','halted')",
            name="ck_crypto_instruments_status",
        ),
    )
    op.create_index(
        "ix_crypto_instruments_venue_product_base",
        "crypto_instruments",
        ["venue", "product", "base_asset"],
    )
    op.create_index(
        "ix_crypto_instruments_base_quote",
        "crypto_instruments",
        ["base_asset", "quote_asset"],
    )


def downgrade() -> None:
    op.drop_index("ix_crypto_instruments_base_quote", table_name="crypto_instruments")
    op.drop_index("ix_crypto_instruments_venue_product_base", table_name="crypto_instruments")
    op.drop_table("crypto_instruments")
```

- [ ] **Step 4: Apply migration and re-run tests**

```bash
uv run alembic upgrade head
uv run pytest tests/services/daily_candles/test_crypto_instruments.py -v
```

Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add alembic/versions/<rev_a>_add_crypto_instruments.py \
        tests/services/daily_candles/test_crypto_instruments.py
git commit -m "feat(rob-284): add crypto_instruments master table"
```

---

### Task 3: `CryptoInstrument` ORM model

**Files:**
- Create: `app/models/crypto_instruments.py`

- [ ] **Step 1: Write the failing model-roundtrip test**

Append to `tests/services/daily_candles/test_crypto_instruments.py`:

```python
@pytest.mark.asyncio
async def test_crypto_instrument_orm_roundtrip(session: AsyncSession) -> None:
    from app.models.crypto_instruments import CryptoInstrument

    inst = CryptoInstrument(
        venue="binance",
        product="spot",
        venue_symbol="BTCUSDT",
        base_asset="BTC",
        quote_asset="USDT",
        status="active",
        precision_price=2,
        precision_amount=5,
        tick_size=0.01,
        lot_size=0.00001,
        min_notional=10,
    )
    session.add(inst)
    await session.flush()
    assert inst.id is not None
    fetched = await session.get(CryptoInstrument, inst.id)
    assert fetched is not None
    assert fetched.venue == "binance"
    assert fetched.metadata is None
```

- [ ] **Step 2: Run, expect FAIL**

```bash
uv run pytest tests/services/daily_candles/test_crypto_instruments.py::test_crypto_instrument_orm_roundtrip -v
```

Expected: FAIL with `ModuleNotFoundError: app.models.crypto_instruments`.

- [ ] **Step 3: Write the model**

```python
"""ROB-284 — crypto instruments ORM model."""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import BigInteger, CheckConstraint, Integer, Numeric, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class CryptoInstrument(Base):
    __tablename__ = "crypto_instruments"
    __table_args__ = (
        UniqueConstraint(
            "venue", "product", "venue_symbol",
            name="uq_crypto_instruments_venue_product_symbol",
        ),
        CheckConstraint(
            "status IN ('active','delisted','halted')",
            name="ck_crypto_instruments_status",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    venue: Mapped[str] = mapped_column(Text, nullable=False)
    product: Mapped[str] = mapped_column(Text, nullable=False)
    venue_symbol: Mapped[str] = mapped_column(Text, nullable=False)
    base_asset: Mapped[str] = mapped_column(Text, nullable=False)
    quote_asset: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="active")
    precision_price: Mapped[int | None] = mapped_column(Integer, nullable=True)
    precision_amount: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tick_size: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    lot_size: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    min_notional: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    listed_at: Mapped[dt.datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    delisted_at: Mapped[dt.datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    metadata: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
```

> **Note:** SQLAlchemy reserves `metadata` on the declarative base. If the runtime complains, rename the attribute to `extra_metadata` and map it to the DB column `metadata` via `mapped_column("metadata", ...)`.

- [ ] **Step 4: Run, expect PASS**

```bash
uv run pytest tests/services/daily_candles/test_crypto_instruments.py -v
```

Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add app/models/crypto_instruments.py tests/services/daily_candles/test_crypto_instruments.py
git commit -m "feat(rob-284): add CryptoInstrument ORM model"
```

---

### Task 4: `crypto_candles_1m` migration (new table)

**Files:**
- Create: `alembic/versions/<rev_b>_add_crypto_candles_1m.py`

- [ ] **Step 1: Write the failing schema + hypertable test**

Create `tests/services/daily_candles/test_crypto_candles_1m.py`:

```python
"""ROB-284 — crypto_candles_1m schema + hypertable."""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.asyncio
async def test_table_columns(session: AsyncSession) -> None:
    result = await session.execute(
        text(
            "SELECT column_name, is_nullable "
            "FROM information_schema.columns "
            "WHERE table_name = 'crypto_candles_1m' "
            "ORDER BY ordinal_position"
        )
    )
    cols = {row.column_name: row.is_nullable for row in result}
    assert cols["instrument_id"] == "NO"
    assert cols["time"] == "NO"
    assert cols["open"] == "NO"
    assert cols["high"] == "NO"
    assert cols["low"] == "NO"
    assert cols["close"] == "NO"
    assert cols["base_volume"] == "NO"
    assert cols["quote_volume"] == "YES"
    assert cols["trade_count"] == "YES"
    assert cols["vwap"] == "YES"
    assert cols["taker_buy_base_volume"] == "YES"
    assert cols["taker_buy_quote_volume"] == "YES"
    assert cols["is_closed"] == "NO"
    assert cols["source"] == "NO"
    assert cols["source_event_at"] == "YES"
    assert cols["ingested_at"] == "NO"


@pytest.mark.asyncio
async def test_hypertable_registered(session: AsyncSession) -> None:
    result = await session.execute(
        text(
            "SELECT count(*) FROM timescaledb_information.hypertables "
            "WHERE hypertable_name = 'crypto_candles_1m'"
        )
    )
    assert result.scalar_one() == 1


@pytest.mark.asyncio
async def test_ohlc_check_rejects_inconsistent(session: AsyncSession) -> None:
    # Pre-seed an instrument.
    await session.execute(
        text(
            "INSERT INTO crypto_instruments "
            "(venue, product, venue_symbol, base_asset, quote_asset, status) "
            "VALUES ('upbit', 'spot', 'KRW-BTC', 'BTC', 'KRW', 'active') "
            "RETURNING id"
        )
    )
    inst_id = (await session.execute(
        text("SELECT id FROM crypto_instruments WHERE venue_symbol='KRW-BTC'")
    )).scalar_one()
    with pytest.raises(IntegrityError):
        await session.execute(
            text(
                "INSERT INTO crypto_candles_1m "
                "(instrument_id, time, open, high, low, close, base_volume, "
                "is_closed, source) "
                "VALUES (:iid, '2026-05-20T00:00:00Z', 100, 50, 60, 70, 1, true, 'test')"
            ),
            {"iid": inst_id},
        )
        await session.flush()
```

- [ ] **Step 2: Run, expect FAIL**

```bash
uv run pytest tests/services/daily_candles/test_crypto_candles_1m.py -v
```

Expected: 3 failed.

- [ ] **Step 3: Write the migration**

```bash
uv run alembic revision -m "add crypto_candles_1m"
```

```python
"""add crypto_candles_1m

Revision ID: <rev_b>
Revises: <rev_a>
"""
from alembic import op
import sqlalchemy as sa


revision = "<rev_b>"
down_revision = "<rev_a>"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "crypto_candles_1m",
        sa.Column("instrument_id", sa.BigInteger(), nullable=False),
        sa.Column("time", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("open", sa.Numeric(), nullable=False),
        sa.Column("high", sa.Numeric(), nullable=False),
        sa.Column("low", sa.Numeric(), nullable=False),
        sa.Column("close", sa.Numeric(), nullable=False),
        sa.Column("base_volume", sa.Numeric(), nullable=False),
        sa.Column("quote_volume", sa.Numeric(), nullable=True),
        sa.Column("trade_count", sa.Integer(), nullable=True),
        sa.Column("vwap", sa.Numeric(), nullable=True),
        sa.Column("taker_buy_base_volume", sa.Numeric(), nullable=True),
        sa.Column("taker_buy_quote_volume", sa.Numeric(), nullable=True),
        sa.Column(
            "is_closed",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("TRUE"),
        ),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("source_event_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "ingested_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["instrument_id"], ["crypto_instruments.id"],
            name="fk_crypto_candles_1m_instrument",
        ),
        sa.PrimaryKeyConstraint("instrument_id", "time", name="pk_crypto_candles_1m"),
        sa.CheckConstraint("base_volume >= 0", name="ck_crypto_candles_1m_base_volume_nn"),
        sa.CheckConstraint(
            "quote_volume IS NULL OR quote_volume >= 0",
            name="ck_crypto_candles_1m_quote_volume_nn",
        ),
        sa.CheckConstraint(
            "trade_count IS NULL OR trade_count >= 0",
            name="ck_crypto_candles_1m_trade_count_nn",
        ),
        sa.CheckConstraint(
            "vwap IS NULL OR vwap >= 0",
            name="ck_crypto_candles_1m_vwap_nn",
        ),
        sa.CheckConstraint("high >= low", name="ck_crypto_candles_1m_high_ge_low"),
        sa.CheckConstraint(
            "high >= open AND high >= close",
            name="ck_crypto_candles_1m_high_ge_oc",
        ),
        sa.CheckConstraint(
            "low <= open AND low <= close",
            name="ck_crypto_candles_1m_low_le_oc",
        ),
    )
    op.execute(
        "SELECT create_hypertable('crypto_candles_1m', 'time', "
        "chunk_time_interval => INTERVAL '1 day')"
    )
    op.create_index(
        "ix_crypto_candles_1m_source_time",
        "crypto_candles_1m",
        ["source", sa.text("time DESC")],
    )


def downgrade() -> None:
    op.drop_index("ix_crypto_candles_1m_source_time", table_name="crypto_candles_1m")
    op.drop_table("crypto_candles_1m")
```

- [ ] **Step 4: Apply and re-run tests**

```bash
uv run alembic upgrade head
uv run pytest tests/services/daily_candles/test_crypto_candles_1m.py -v
```

Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add alembic/versions/<rev_b>_add_crypto_candles_1m.py \
        tests/services/daily_candles/test_crypto_candles_1m.py
git commit -m "feat(rob-284): add crypto_candles_1m hypertable"
```

---

### Task 5: `CryptoCandle1m` ORM model + `MinuteCandlesRepository`

**Files:**
- Create: `app/models/crypto_candles.py` (1m portion)
- Create: `app/services/minute_candles/__init__.py`, `app/services/minute_candles/repository.py`

- [ ] **Step 1: Write the failing repository idempotency test**

Append to `tests/services/daily_candles/test_crypto_candles_1m.py`:

```python
@pytest.mark.asyncio
async def test_minute_repository_idempotent_upsert(session: AsyncSession) -> None:
    from app.models.crypto_instruments import CryptoInstrument
    from app.services.minute_candles.repository import (
        MinuteCandleRow,
        MinuteCandlesRepository,
    )

    inst = CryptoInstrument(
        venue="binance", product="spot", venue_symbol="BTCUSDT",
        base_asset="BTC", quote_asset="USDT", status="active",
    )
    session.add(inst)
    await session.flush()

    repo = MinuteCandlesRepository(session=session)
    row = MinuteCandleRow(
        instrument_id=inst.id,
        time_utc=dt.datetime(2026, 5, 20, tzinfo=dt.timezone.utc),
        open=100, high=105, low=99, close=103,
        base_volume=10, quote_volume=1030,
        is_closed=True, source="binance_sdk_ws",
    )
    first = await repo.upsert_rows(rows=[row])
    second = await repo.upsert_rows(rows=[row])
    assert first == 1
    # Second insert is a no-op (closed candle not overwritten by same source).
    cnt = (await session.execute(
        text("SELECT count(*) FROM crypto_candles_1m WHERE instrument_id = :i"),
        {"i": inst.id},
    )).scalar_one()
    assert cnt == 1


@pytest.mark.asyncio
async def test_cross_venue_same_bucket_coexistence(session: AsyncSession) -> None:
    """ROB-284 — 4 distinct instruments at same time bucket must not collide."""
    from app.models.crypto_instruments import CryptoInstrument
    from app.services.minute_candles.repository import (
        MinuteCandleRow, MinuteCandlesRepository,
    )

    instruments = [
        ("upbit", "spot", "KRW-BTC", "BTC", "KRW"),
        ("binance", "spot", "BTCUSDT", "BTC", "USDT"),
        ("binance", "usdm_futures", "BTCUSDT", "BTC", "USDT"),
        ("alpaca", "paper", "BTC/USD", "BTC", "USD"),
    ]
    ids = []
    for venue, product, sym, base, quote in instruments:
        inst = CryptoInstrument(
            venue=venue, product=product, venue_symbol=sym,
            base_asset=base, quote_asset=quote, status="active",
        )
        session.add(inst)
        await session.flush()
        ids.append(inst.id)

    repo = MinuteCandlesRepository(session=session)
    t = dt.datetime(2026, 5, 20, tzinfo=dt.timezone.utc)
    rows = [
        MinuteCandleRow(
            instrument_id=i, time_utc=t,
            open=100, high=101, low=99, close=100, base_volume=1,
            is_closed=True, source="test",
        )
        for i in ids
    ]
    count = await repo.upsert_rows(rows=rows)
    assert count == 4
```

- [ ] **Step 2: Run, expect FAIL (modules missing)**

```bash
uv run pytest tests/services/daily_candles/test_crypto_candles_1m.py -v
```

Expected: 2 failed.

- [ ] **Step 3: Write the ORM model (1m portion)**

Create `app/models/crypto_candles.py`:

```python
"""ROB-284 — crypto candle ORM models (1m + 1d)."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    BigInteger, Boolean, CheckConstraint, ForeignKey, Integer,
    Numeric, PrimaryKeyConstraint, Text,
)
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class CryptoCandle1m(Base):
    __tablename__ = "crypto_candles_1m"
    __table_args__ = (
        PrimaryKeyConstraint("instrument_id", "time", name="pk_crypto_candles_1m"),
        CheckConstraint("base_volume >= 0", name="ck_crypto_candles_1m_base_volume_nn"),
        CheckConstraint("high >= low", name="ck_crypto_candles_1m_high_ge_low"),
    )

    instrument_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("crypto_instruments.id"), nullable=False
    )
    time: Mapped[dt.datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    open: Mapped[float] = mapped_column(Numeric, nullable=False)
    high: Mapped[float] = mapped_column(Numeric, nullable=False)
    low: Mapped[float] = mapped_column(Numeric, nullable=False)
    close: Mapped[float] = mapped_column(Numeric, nullable=False)
    base_volume: Mapped[float] = mapped_column(Numeric, nullable=False)
    quote_volume: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    trade_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    vwap: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    taker_buy_base_volume: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    taker_buy_quote_volume: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    is_closed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    source_event_at: Mapped[dt.datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    ingested_at: Mapped[dt.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
```

Create `app/services/minute_candles/__init__.py` (empty) and `app/services/minute_candles/repository.py`:

```python
"""ROB-284 — DB boundary for crypto 1m candle store."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import cast

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import TextClause


@dataclass(frozen=True, slots=True)
class MinuteCandleRow:
    instrument_id: int
    time_utc: datetime
    open: float
    high: float
    low: float
    close: float
    base_volume: float
    quote_volume: float | None = None
    trade_count: int | None = None
    vwap: float | None = None
    taker_buy_base_volume: float | None = None
    taker_buy_quote_volume: float | None = None
    is_closed: bool = True
    source: str = ""
    source_event_at: datetime | None = None


class _RowcountResult:
    rowcount: int | None


class MinuteCandlesRepository:
    """Writes to crypto_candles_1m via instrument_id.

    Idempotent: a closed candle is never overwritten by a less-trustworthy
    source. The upsert WHERE clause mirrors DailyCandlesRepository's behavior.
    """

    def __init__(self, *, session: AsyncSession) -> None:
        self._session = session

    @property
    def session(self) -> AsyncSession:
        return self._session

    async def upsert_rows(self, *, rows: list[MinuteCandleRow]) -> int:
        if not rows:
            return 0
        sql = self._build_upsert()
        payload = [
            {
                "instrument_id": r.instrument_id,
                "time": r.time_utc,
                "open": r.open,
                "high": r.high,
                "low": r.low,
                "close": r.close,
                "base_volume": r.base_volume,
                "quote_volume": r.quote_volume,
                "trade_count": r.trade_count,
                "vwap": r.vwap,
                "taker_buy_base_volume": r.taker_buy_base_volume,
                "taker_buy_quote_volume": r.taker_buy_quote_volume,
                "is_closed": r.is_closed,
                "source": r.source,
                "source_event_at": r.source_event_at,
            }
            for r in rows
        ]
        result = cast(
            "_RowcountResult",
            cast(object, await self._session.execute(sql, payload)),
        )
        return max(int(result.rowcount or 0), 0)

    @staticmethod
    def _build_upsert() -> TextClause:
        return text(
            """
            INSERT INTO public.crypto_candles_1m (
                instrument_id, time, open, high, low, close,
                base_volume, quote_volume, trade_count, vwap,
                taker_buy_base_volume, taker_buy_quote_volume,
                is_closed, source, source_event_at
            ) VALUES (
                :instrument_id, :time, :open, :high, :low, :close,
                :base_volume, :quote_volume, :trade_count, :vwap,
                :taker_buy_base_volume, :taker_buy_quote_volume,
                :is_closed, :source, :source_event_at
            )
            ON CONFLICT (instrument_id, time) DO UPDATE
            SET open                   = EXCLUDED.open,
                high                   = EXCLUDED.high,
                low                    = EXCLUDED.low,
                close                  = EXCLUDED.close,
                base_volume            = EXCLUDED.base_volume,
                quote_volume           = EXCLUDED.quote_volume,
                trade_count            = EXCLUDED.trade_count,
                vwap                   = EXCLUDED.vwap,
                taker_buy_base_volume  = EXCLUDED.taker_buy_base_volume,
                taker_buy_quote_volume = EXCLUDED.taker_buy_quote_volume,
                is_closed              = EXCLUDED.is_closed,
                source                 = EXCLUDED.source,
                source_event_at        = EXCLUDED.source_event_at,
                ingested_at            = now()
            WHERE
                -- Never overwrite a closed candle from the same source.
                NOT (public.crypto_candles_1m.is_closed = TRUE
                     AND EXCLUDED.is_closed = TRUE
                     AND public.crypto_candles_1m.source = EXCLUDED.source)
            """
        )
```

- [ ] **Step 4: Run, expect PASS**

```bash
uv run pytest tests/services/daily_candles/test_crypto_candles_1m.py -v
```

Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add app/models/crypto_candles.py \
        app/services/minute_candles/ \
        tests/services/daily_candles/test_crypto_candles_1m.py
git commit -m "feat(rob-284): add CryptoCandle1m model + MinuteCandlesRepository"
```

---

### Task 6: Pre-migration backup runbook update

**Files:**
- Modify: `docs/runbooks/daily-candles-store.md`

- [ ] **Step 1: Add the operator backup procedure**

Append a new section to `docs/runbooks/daily-candles-store.md`:

```markdown
## ROB-284 pre-migration backup (one-time, before alembic upgrade)

The `crypto_candles_1d` in-place migration drops legacy `symbol` / `market`
columns. Before running `alembic upgrade head` on any environment with
existing crypto candle data, take a backup table:

```sql
-- Run as DB superuser on the target environment.
CREATE TABLE crypto_candles_1d_pre_rob283 AS
SELECT * FROM crypto_candles_1d;

-- Verify row count matches.
SELECT
  (SELECT COUNT(*) FROM crypto_candles_1d) AS live,
  (SELECT COUNT(*) FROM crypto_candles_1d_pre_rob283) AS backup;
```

If `live != backup`, abort the migration. If they match, proceed:

```bash
uv run alembic upgrade head
```

To roll back manually after a failed migration:

```sql
DROP TABLE crypto_candles_1d;
ALTER TABLE crypto_candles_1d_pre_rob283 RENAME TO crypto_candles_1d;
-- Restore Timescale hypertable registration:
SELECT create_hypertable('crypto_candles_1d', 'time',
  chunk_time_interval => INTERVAL '90 days', migrate_data => TRUE);
```

Remove the backup table only after at least one full week of successful
operation on the new schema:

```sql
DROP TABLE crypto_candles_1d_pre_rob283;
```
```

- [ ] **Step 2: Commit**

```bash
git add docs/runbooks/daily-candles-store.md
git commit -m "docs(rob-284): add crypto_candles_1d pre-migration backup procedure"
```

---

### Task 7: `crypto_candles_1d` migration step 1 — add nullable columns

**Files:**
- Create: `alembic/versions/<rev_c>_migrate_crypto_candles_1d_step1_add_columns.py`

The migration is split into three Alembic revisions so each step is reversible independently and so the backfill (step 2) can be re-run without re-running step 1's DDL.

- [ ] **Step 1: Write the failing intermediate-state test**

Create `tests/services/daily_candles/test_crypto_candles_1d_migration.py`:

```python
"""ROB-284 — crypto_candles_1d in-place migration to instrument-FK shape."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.asyncio
async def test_step1_adds_nullable_columns(session: AsyncSession) -> None:
    result = await session.execute(
        text(
            "SELECT column_name, is_nullable "
            "FROM information_schema.columns "
            "WHERE table_name = 'crypto_candles_1d'"
        )
    )
    cols = {row.column_name: row.is_nullable for row in result}
    # Old columns still present at this stage:
    assert "symbol" in cols
    assert "market" in cols
    # New columns added by step 1 — all nullable initially:
    assert cols.get("instrument_id") == "YES"
    assert cols.get("base_volume") == "YES"
    assert cols.get("quote_volume") == "YES"
    assert cols.get("is_closed") == "YES"
    assert cols.get("source_event_at") == "YES"
```

- [ ] **Step 2: Run, expect FAIL**

```bash
uv run pytest tests/services/daily_candles/test_crypto_candles_1d_migration.py::test_step1_adds_nullable_columns -v
```

Expected: FAIL (new columns don't exist).

- [ ] **Step 3: Write the migration**

```bash
uv run alembic revision -m "migrate crypto_candles_1d step1 add columns"
```

```python
"""migrate crypto_candles_1d step1 add columns

Revision ID: <rev_c>
Revises: <rev_b>
"""
from alembic import op
import sqlalchemy as sa


revision = "<rev_c>"
down_revision = "<rev_b>"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "crypto_candles_1d",
        sa.Column("instrument_id", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "crypto_candles_1d",
        sa.Column("base_volume", sa.Numeric(), nullable=True),
    )
    op.add_column(
        "crypto_candles_1d",
        sa.Column("quote_volume", sa.Numeric(), nullable=True),
    )
    op.add_column(
        "crypto_candles_1d",
        sa.Column(
            "is_closed",
            sa.Boolean(),
            nullable=True,
            server_default=sa.text("TRUE"),
        ),
    )
    op.add_column(
        "crypto_candles_1d",
        sa.Column("source_event_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_crypto_candles_1d_instrument_time",
        "crypto_candles_1d",
        ["instrument_id", sa.text("time DESC")],
    )


def downgrade() -> None:
    op.drop_index("ix_crypto_candles_1d_instrument_time", table_name="crypto_candles_1d")
    op.drop_column("crypto_candles_1d", "source_event_at")
    op.drop_column("crypto_candles_1d", "is_closed")
    op.drop_column("crypto_candles_1d", "quote_volume")
    op.drop_column("crypto_candles_1d", "base_volume")
    op.drop_column("crypto_candles_1d", "instrument_id")
```

- [ ] **Step 4: Apply and re-run the step1 test**

```bash
uv run alembic upgrade head
uv run pytest tests/services/daily_candles/test_crypto_candles_1d_migration.py::test_step1_adds_nullable_columns -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add alembic/versions/<rev_c>_*.py \
        tests/services/daily_candles/test_crypto_candles_1d_migration.py
git commit -m "feat(rob-284): crypto_candles_1d migration step 1 — add nullable columns"
```

---

### Task 8: `crypto_candles_1d` migration step 2 — seed instruments + backfill

**Files:**
- Create: `alembic/versions/<rev_d>_migrate_crypto_candles_1d_step2_backfill.py`

This step is **idempotent and re-runnable**. If row counts don't match expectations, it can be rerun safely.

- [ ] **Step 1: Write the failing backfill-correctness test**

Append to `tests/services/daily_candles/test_crypto_candles_1d_migration.py`:

```python
@pytest.mark.asyncio
async def test_step2_backfill_populates_instrument_id_and_volumes(
    session: AsyncSession,
) -> None:
    # Sanity: every existing crypto_candles_1d row must have instrument_id
    # populated after step 2 (and is_closed set, base_volume copied from volume).
    rows_total = (await session.execute(
        text("SELECT count(*) FROM crypto_candles_1d")
    )).scalar_one()
    rows_with_iid = (await session.execute(
        text("SELECT count(*) FROM crypto_candles_1d WHERE instrument_id IS NOT NULL")
    )).scalar_one()
    rows_with_base_vol = (await session.execute(
        text("SELECT count(*) FROM crypto_candles_1d WHERE base_volume IS NOT NULL")
    )).scalar_one()
    rows_closed = (await session.execute(
        text("SELECT count(*) FROM crypto_candles_1d WHERE is_closed IS TRUE")
    )).scalar_one()
    assert rows_total > 0, "Test database must contain crypto candle fixture rows"
    assert rows_with_iid == rows_total
    assert rows_with_base_vol == rows_total
    assert rows_closed == rows_total


@pytest.mark.asyncio
async def test_step2_creates_one_instrument_per_distinct_pair(
    session: AsyncSession,
) -> None:
    # For every distinct (legacy_market, legacy_symbol) seen in pre-migration
    # crypto_candles_1d, exactly one crypto_instruments row exists.
    result = await session.execute(
        text(
            "SELECT COUNT(DISTINCT instrument_id) FROM crypto_candles_1d"
        )
    )
    distinct_iids = result.scalar_one()
    result = await session.execute(
        text("SELECT count(*) FROM crypto_instruments WHERE venue = 'upbit'")
    )
    upbit_instruments = result.scalar_one()
    assert distinct_iids == upbit_instruments
```

> **Test fixture note:** The migration tests require a fixture that seeds at least a few Upbit KRW pre-migration candle rows before applying step 1 + step 2. Add a session-scoped fixture in `tests/services/daily_candles/conftest.py` if one does not exist; the fixture should insert ~5 rows for `KRW-BTC` and `KRW-ETH` at distinct times prior to alembic upgrade.

- [ ] **Step 2: Run, expect FAIL**

```bash
uv run pytest tests/services/daily_candles/test_crypto_candles_1d_migration.py -v -k step2
```

Expected: FAIL (backfill has not run).

- [ ] **Step 3: Write the migration**

```bash
uv run alembic revision -m "migrate crypto_candles_1d step2 backfill"
```

```python
"""migrate crypto_candles_1d step2 backfill

Revision ID: <rev_d>
Revises: <rev_c>

ROB-284 step 2 — idempotent backfill:
  - For every distinct (market, symbol) currently in crypto_candles_1d, ensure
    a crypto_instruments row exists. Today this is Upbit KRW pairs only
    (market='upbit_krw', symbol like 'KRW-XXX').
  - Populate instrument_id by JOIN on (market, symbol) → derived
    (venue, product, venue_symbol).
  - Copy volume → base_volume, value → quote_volume.
  - Mark all existing rows is_closed = TRUE.
"""
from alembic import op


revision = "<rev_d>"
down_revision = "<rev_c>"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Seed crypto_instruments for every distinct (market, symbol).
    #
    # Today the only producer is the Upbit KRW path: market='upbit_krw',
    # symbol like 'KRW-XXX'. We translate that into (venue='upbit',
    # product='spot', venue_symbol=symbol, base_asset=substring after 'KRW-',
    # quote_asset='KRW'). The query is idempotent via ON CONFLICT DO NOTHING.
    op.execute(
        """
        INSERT INTO crypto_instruments
            (venue, product, venue_symbol, base_asset, quote_asset, status)
        SELECT DISTINCT
            CASE WHEN c.market = 'upbit_krw' THEN 'upbit' ELSE c.market END
              AS venue,
            'spot' AS product,
            c.symbol AS venue_symbol,
            CASE
                WHEN c.symbol LIKE 'KRW-%' THEN substring(c.symbol from 5)
                ELSE c.symbol
            END AS base_asset,
            CASE
                WHEN c.symbol LIKE 'KRW-%' THEN 'KRW'
                ELSE 'UNKNOWN'
            END AS quote_asset,
            'active' AS status
        FROM crypto_candles_1d c
        ON CONFLICT (venue, product, venue_symbol) DO NOTHING
        """
    )

    # 2. Backfill instrument_id, base_volume, quote_volume, is_closed.
    op.execute(
        """
        UPDATE crypto_candles_1d c
        SET instrument_id = i.id,
            base_volume   = COALESCE(c.base_volume, c.volume),
            quote_volume  = COALESCE(c.quote_volume, c.value),
            is_closed     = COALESCE(c.is_closed, TRUE)
        FROM crypto_instruments i
        WHERE i.venue_symbol = c.symbol
          AND i.venue = CASE WHEN c.market = 'upbit_krw' THEN 'upbit' ELSE c.market END
          AND i.product = 'spot'
          AND c.instrument_id IS NULL
        """
    )


def downgrade() -> None:
    # Reverse the backfill: clear the columns we populated. The instruments
    # themselves are kept (cheap, useful for other paths).
    op.execute(
        """
        UPDATE crypto_candles_1d
        SET instrument_id = NULL,
            base_volume   = NULL,
            quote_volume  = NULL,
            is_closed     = NULL
        """
    )
```

- [ ] **Step 4: Apply and re-run tests**

```bash
uv run alembic upgrade head
uv run pytest tests/services/daily_candles/test_crypto_candles_1d_migration.py -v -k step2
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add alembic/versions/<rev_d>_*.py \
        tests/services/daily_candles/test_crypto_candles_1d_migration.py \
        tests/services/daily_candles/conftest.py
git commit -m "feat(rob-284): crypto_candles_1d migration step 2 — seed + backfill"
```

---

### Task 9: `crypto_candles_1d` migration step 3 — enforce NOT NULL, drop legacy, add CHECKs

**Files:**
- Create: `alembic/versions/<rev_e>_migrate_crypto_candles_1d_step3_finalize.py`

This step is **destructive**: drops `symbol` / `market` columns. The pre-migration backup from Task 6 is the recovery path.

- [ ] **Step 1: Write the failing post-migration shape test**

Append to `tests/services/daily_candles/test_crypto_candles_1d_migration.py`:

```python
@pytest.mark.asyncio
async def test_step3_final_shape(session: AsyncSession) -> None:
    result = await session.execute(
        text(
            "SELECT column_name, is_nullable "
            "FROM information_schema.columns "
            "WHERE table_name = 'crypto_candles_1d'"
        )
    )
    cols = {row.column_name: row.is_nullable for row in result}
    # Legacy columns are gone.
    assert "symbol" not in cols
    assert "market" not in cols
    assert "volume" not in cols
    assert "value" not in cols
    # New shape:
    assert cols["instrument_id"] == "NO"
    assert cols["base_volume"] == "NO"
    assert cols["is_closed"] == "NO"
    assert cols["quote_volume"] == "YES"


@pytest.mark.asyncio
async def test_step3_primary_key_is_instrument_time(session: AsyncSession) -> None:
    result = await session.execute(
        text(
            "SELECT a.attname "
            "FROM pg_index i JOIN pg_attribute a "
            "  ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey) "
            "WHERE i.indrelid = 'public.crypto_candles_1d'::regclass "
            "  AND i.indisprimary"
        )
    )
    pk_cols = {row.attname for row in result}
    assert pk_cols == {"instrument_id", "time"}


@pytest.mark.asyncio
async def test_step3_checks_present(session: AsyncSession) -> None:
    result = await session.execute(
        text(
            "SELECT conname FROM pg_constraint "
            "WHERE conrelid = 'public.crypto_candles_1d'::regclass "
            "  AND contype = 'c'"
        )
    )
    check_names = {row.conname for row in result}
    expected = {
        "ck_crypto_candles_1d_base_volume_nn",
        "ck_crypto_candles_1d_high_ge_low",
        "ck_crypto_candles_1d_high_ge_oc",
        "ck_crypto_candles_1d_low_le_oc",
    }
    assert expected <= check_names
```

- [ ] **Step 2: Run, expect FAIL**

```bash
uv run pytest tests/services/daily_candles/test_crypto_candles_1d_migration.py -v -k step3
```

Expected: FAIL.

- [ ] **Step 3: Write the migration**

```bash
uv run alembic revision -m "migrate crypto_candles_1d step3 finalize"
```

```python
"""migrate crypto_candles_1d step3 finalize

Revision ID: <rev_e>
Revises: <rev_d>

ROB-284 step 3 — destructive finalize. Operator MUST have taken the
crypto_candles_1d_pre_rob283 backup (see docs/runbooks/daily-candles-store.md)
before running this revision.
"""
from alembic import op
import sqlalchemy as sa


revision = "<rev_e>"
down_revision = "<rev_d>"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Safety check: refuse to drop legacy columns if any row has
    # instrument_id NULL or base_volume NULL — the step 2 backfill is incomplete.
    connection = op.get_bind()
    incomplete = connection.execute(
        sa.text(
            "SELECT count(*) FROM crypto_candles_1d "
            "WHERE instrument_id IS NULL OR base_volume IS NULL OR is_closed IS NULL"
        )
    ).scalar_one()
    if incomplete > 0:
        raise RuntimeError(
            f"ROB-284 step 3 refused: {incomplete} rows in crypto_candles_1d "
            "have NULL instrument_id / base_volume / is_closed. "
            "Re-run step 2 backfill (or restore from backup) first."
        )

    # Enforce NOT NULL.
    op.alter_column("crypto_candles_1d", "instrument_id", nullable=False)
    op.alter_column("crypto_candles_1d", "base_volume", nullable=False)
    op.alter_column("crypto_candles_1d", "is_closed", nullable=False)

    # Add FK now that instrument_id is fully populated.
    op.create_foreign_key(
        "fk_crypto_candles_1d_instrument",
        "crypto_candles_1d", "crypto_instruments",
        ["instrument_id"], ["id"],
    )

    # Drop legacy columns (symbol, market, volume, value).
    # Drop legacy indexes/uniques first.
    op.drop_constraint(
        "uq_crypto_candles_1d_time_symbol_market",
        "crypto_candles_1d",
        type_="unique",
    )
    op.drop_index("ix_crypto_candles_1d_symbol_market_time", table_name="crypto_candles_1d")
    op.drop_column("crypto_candles_1d", "market")
    op.drop_column("crypto_candles_1d", "symbol")
    op.drop_column("crypto_candles_1d", "volume")
    op.drop_column("crypto_candles_1d", "value")

    # New PK = (instrument_id, time). Timescale hypertable accepts a composite
    # PK that includes the partitioning column.
    op.execute(
        "ALTER TABLE crypto_candles_1d "
        "ADD CONSTRAINT pk_crypto_candles_1d PRIMARY KEY (instrument_id, time)"
    )

    # CHECK constraints (OHLC sanity + non-negative).
    op.create_check_constraint(
        "ck_crypto_candles_1d_base_volume_nn",
        "crypto_candles_1d",
        "base_volume >= 0",
    )
    op.create_check_constraint(
        "ck_crypto_candles_1d_quote_volume_nn",
        "crypto_candles_1d",
        "quote_volume IS NULL OR quote_volume >= 0",
    )
    op.create_check_constraint(
        "ck_crypto_candles_1d_high_ge_low",
        "crypto_candles_1d",
        "high >= low",
    )
    op.create_check_constraint(
        "ck_crypto_candles_1d_high_ge_oc",
        "crypto_candles_1d",
        "high >= open AND high >= close",
    )
    op.create_check_constraint(
        "ck_crypto_candles_1d_low_le_oc",
        "crypto_candles_1d",
        "low <= open AND low <= close",
    )


def downgrade() -> None:
    # Restore legacy shape from JOIN. Requires crypto_instruments still present.
    op.drop_constraint("ck_crypto_candles_1d_low_le_oc", "crypto_candles_1d", type_="check")
    op.drop_constraint("ck_crypto_candles_1d_high_ge_oc", "crypto_candles_1d", type_="check")
    op.drop_constraint("ck_crypto_candles_1d_high_ge_low", "crypto_candles_1d", type_="check")
    op.drop_constraint("ck_crypto_candles_1d_quote_volume_nn", "crypto_candles_1d", type_="check")
    op.drop_constraint("ck_crypto_candles_1d_base_volume_nn", "crypto_candles_1d", type_="check")
    op.execute("ALTER TABLE crypto_candles_1d DROP CONSTRAINT pk_crypto_candles_1d")
    op.add_column("crypto_candles_1d", sa.Column("value", sa.Numeric(), nullable=True))
    op.add_column("crypto_candles_1d", sa.Column("volume", sa.Numeric(), nullable=True))
    op.add_column("crypto_candles_1d", sa.Column("symbol", sa.Text(), nullable=True))
    op.add_column("crypto_candles_1d", sa.Column("market", sa.Text(), nullable=True))
    op.execute(
        """
        UPDATE crypto_candles_1d c
        SET symbol = i.venue_symbol,
            market = CASE WHEN i.venue = 'upbit' THEN 'upbit_krw' ELSE i.venue END,
            volume = c.base_volume,
            value  = c.quote_volume
        FROM crypto_instruments i
        WHERE i.id = c.instrument_id
        """
    )
    op.alter_column("crypto_candles_1d", "symbol", nullable=False)
    op.alter_column("crypto_candles_1d", "market", nullable=False)
    op.alter_column("crypto_candles_1d", "volume", nullable=False)
    op.alter_column("crypto_candles_1d", "instrument_id", nullable=True)
    op.alter_column("crypto_candles_1d", "is_closed", nullable=True)
    op.drop_constraint("fk_crypto_candles_1d_instrument", "crypto_candles_1d", type_="foreignkey")
    op.create_index(
        "ix_crypto_candles_1d_symbol_market_time",
        "crypto_candles_1d",
        ["symbol", "market", sa.text("time DESC")],
    )
    op.create_unique_constraint(
        "uq_crypto_candles_1d_time_symbol_market",
        "crypto_candles_1d",
        ["time", "symbol", "market"],
    )
```

- [ ] **Step 4: Apply and re-run tests**

```bash
uv run alembic upgrade head
uv run pytest tests/services/daily_candles/test_crypto_candles_1d_migration.py -v
```

Expected: all step1/2/3 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add alembic/versions/<rev_e>_*.py \
        tests/services/daily_candles/test_crypto_candles_1d_migration.py
git commit -m "feat(rob-284): crypto_candles_1d migration step 3 — finalize NOT NULL + drop legacy columns"
```

---

### Task 10: `CryptoCandle1d` ORM model

**Files:**
- Modify: `app/models/crypto_candles.py` (add 1d class)

- [ ] **Step 1: Write the failing ORM-roundtrip test**

Append to `tests/services/daily_candles/test_crypto_candles_1d_migration.py`:

```python
@pytest.mark.asyncio
async def test_daily_candle_orm_roundtrip(session: AsyncSession) -> None:
    import datetime as dt

    from app.models.crypto_candles import CryptoCandle1d
    from app.models.crypto_instruments import CryptoInstrument

    inst = CryptoInstrument(
        venue="binance", product="spot", venue_symbol="ETHUSDT",
        base_asset="ETH", quote_asset="USDT", status="active",
    )
    session.add(inst)
    await session.flush()

    candle = CryptoCandle1d(
        instrument_id=inst.id,
        time=dt.datetime(2026, 5, 20, tzinfo=dt.timezone.utc),
        open=3000, high=3100, low=2950, close=3050,
        base_volume=42.5, quote_volume=128000, is_closed=True, source="test",
    )
    session.add(candle)
    await session.flush()
    fetched = await session.get(
        CryptoCandle1d,
        (inst.id, dt.datetime(2026, 5, 20, tzinfo=dt.timezone.utc)),
    )
    assert fetched is not None
    assert float(fetched.close) == 3050.0
```

- [ ] **Step 2: Run, expect FAIL**

```bash
uv run pytest tests/services/daily_candles/test_crypto_candles_1d_migration.py::test_daily_candle_orm_roundtrip -v
```

- [ ] **Step 3: Add the `CryptoCandle1d` model**

Append to `app/models/crypto_candles.py`:

```python
class CryptoCandle1d(Base):
    __tablename__ = "crypto_candles_1d"
    __table_args__ = (
        PrimaryKeyConstraint("instrument_id", "time", name="pk_crypto_candles_1d"),
    )

    instrument_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("crypto_instruments.id"), nullable=False
    )
    time: Mapped[dt.datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    open: Mapped[float] = mapped_column(Numeric, nullable=False)
    high: Mapped[float] = mapped_column(Numeric, nullable=False)
    low: Mapped[float] = mapped_column(Numeric, nullable=False)
    close: Mapped[float] = mapped_column(Numeric, nullable=False)
    base_volume: Mapped[float] = mapped_column(Numeric, nullable=False)
    quote_volume: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    is_closed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    source_event_at: Mapped[dt.datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    ingested_at: Mapped[dt.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
```

- [ ] **Step 4: Run, expect PASS**

```bash
uv run pytest tests/services/daily_candles/test_crypto_candles_1d_migration.py -v
```

- [ ] **Step 5: Commit**

```bash
git add app/models/crypto_candles.py tests/services/daily_candles/test_crypto_candles_1d_migration.py
git commit -m "feat(rob-284): add CryptoCandle1d ORM model"
```

---

### Task 11: `DailyCandlesRepository` crypto path uses `instrument_id`

**Files:**
- Modify: `app/services/daily_candles/repository.py`

The KR/US partitions still use the legacy `(symbol, exchange/venue)` shape — only the crypto path migrates. The repository must resolve `(symbol, market)` → `instrument_id` at write time.

- [ ] **Step 1: Write the failing repository test for the crypto path**

Create `tests/services/daily_candles/test_repository_crypto_path.py`:

```python
"""ROB-284 — DailyCandlesRepository crypto path writes via instrument_id."""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.crypto_instruments import CryptoInstrument
from app.services.daily_candles.repository import (
    DailyCandleRow,
    DailyCandlesRepository,
    MarketKey,
)


@pytest.mark.asyncio
async def test_crypto_upsert_writes_via_instrument_id(session: AsyncSession) -> None:
    inst = CryptoInstrument(
        venue="upbit", product="spot", venue_symbol="KRW-SOL",
        base_asset="SOL", quote_asset="KRW", status="active",
    )
    session.add(inst)
    await session.flush()

    repo = DailyCandlesRepository(session=session)
    row = DailyCandleRow(
        time_utc=dt.datetime(2026, 5, 20, tzinfo=dt.timezone.utc),
        symbol="KRW-SOL",
        partition="upbit_krw",
        open=100, high=110, low=95, close=105,
        adj_close=None,
        volume=12.5, value=1300,
        source="upbit",
    )
    count = await repo.upsert_rows(market=MarketKey.CRYPTO, rows=[row])
    assert count == 1

    result = await session.execute(
        text(
            "SELECT instrument_id, base_volume, quote_volume, is_closed, source "
            "FROM crypto_candles_1d WHERE instrument_id = :i"
        ),
        {"i": inst.id},
    )
    stored = result.one()
    assert stored.instrument_id == inst.id
    assert float(stored.base_volume) == 12.5
    assert float(stored.quote_volume) == 1300
    assert stored.is_closed is True
    assert stored.source == "upbit"


@pytest.mark.asyncio
async def test_crypto_upsert_raises_for_unknown_pair(session: AsyncSession) -> None:
    repo = DailyCandlesRepository(session=session)
    row = DailyCandleRow(
        time_utc=dt.datetime(2026, 5, 20, tzinfo=dt.timezone.utc),
        symbol="KRW-NEWCOIN", partition="upbit_krw",
        open=1, high=1, low=1, close=1, adj_close=None,
        volume=1, value=1, source="upbit",
    )
    with pytest.raises(LookupError):
        await repo.upsert_rows(market=MarketKey.CRYPTO, rows=[row])
```

- [ ] **Step 2: Run, expect FAIL**

```bash
uv run pytest tests/services/daily_candles/test_repository_crypto_path.py -v
```

Expected: 2 failed.

- [ ] **Step 3: Modify the repository**

Edit `app/services/daily_candles/repository.py`. Add a `_resolve_instrument` helper and split the crypto write path:

```python
# Inside DailyCandlesRepository class:

async def _resolve_instrument_id(
    self, *, symbol: str, partition: str
) -> int:
    """Translate legacy (symbol, market) → instrument_id.

    Today only Upbit KRW is producing crypto rows; (partition='upbit_krw',
    symbol='KRW-XXX') maps to (venue='upbit', product='spot', venue_symbol=symbol).
    Children B/C will add Binance/Alpaca mappings.
    """
    venue = "upbit" if partition == "upbit_krw" else partition.split("_")[0]
    sql = text(
        "SELECT id FROM crypto_instruments "
        "WHERE venue = :v AND product = 'spot' AND venue_symbol = :s "
        "LIMIT 1"
    )
    result = await self._session.execute(sql, {"v": venue, "s": symbol})
    row = result.first()
    if row is None:
        raise LookupError(
            f"No crypto_instruments row for venue={venue!r} symbol={symbol!r}; "
            "seed the instrument before writing candles."
        )
    return int(row.id)

async def _upsert_crypto_rows(self, *, rows: list[DailyCandleRow]) -> int:
    if not rows:
        return 0
    payload: list[dict[str, object]] = []
    for row in rows:
        iid = await self._resolve_instrument_id(
            symbol=row.symbol, partition=row.partition
        )
        payload.append(
            {
                "instrument_id": iid,
                "time": row.time_utc,
                "open": row.open,
                "high": row.high,
                "low": row.low,
                "close": row.close,
                "base_volume": row.volume,
                "quote_volume": row.value,
                "is_closed": True,
                "source": row.source,
            }
        )
    sql = text(
        """
        INSERT INTO public.crypto_candles_1d (
            instrument_id, time, open, high, low, close,
            base_volume, quote_volume, is_closed, source
        ) VALUES (
            :instrument_id, :time, :open, :high, :low, :close,
            :base_volume, :quote_volume, :is_closed, :source
        )
        ON CONFLICT (instrument_id, time) DO UPDATE
        SET open         = EXCLUDED.open,
            high         = EXCLUDED.high,
            low          = EXCLUDED.low,
            close        = EXCLUDED.close,
            base_volume  = EXCLUDED.base_volume,
            quote_volume = EXCLUDED.quote_volume,
            is_closed    = EXCLUDED.is_closed,
            source       = EXCLUDED.source,
            ingested_at  = now()
        WHERE
            NOT (public.crypto_candles_1d.is_closed = TRUE
                 AND EXCLUDED.is_closed = TRUE
                 AND public.crypto_candles_1d.source = EXCLUDED.source)
        """
    )
    result = cast(
        "_RowcountResult",
        cast(object, await self._session.execute(sql, payload)),
    )
    return max(int(result.rowcount or 0), 0)
```

Branch in `upsert_rows`:

```python
async def upsert_rows(
    self, *, market: MarketKey, rows: list[DailyCandleRow]
) -> int:
    if market == MarketKey.CRYPTO:
        return await self._upsert_crypto_rows(rows=rows)
    # ... existing KR/US logic unchanged ...
```

Also update `fetch_recent` and `latest_time_utc` for the crypto path to query via `instrument_id` (mirror the helper above and use `JOIN crypto_instruments`).

- [ ] **Step 4: Run, expect PASS**

```bash
uv run pytest tests/services/daily_candles/test_repository_crypto_path.py -v
uv run pytest tests/services/daily_candles/ -v  # full module — KR/US paths must still pass
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add app/services/daily_candles/repository.py \
        tests/services/daily_candles/test_repository_crypto_path.py
git commit -m "feat(rob-284): DailyCandlesRepository crypto path writes via instrument_id"
```

---

### Task 12: Round-trip downgrade test + rollback runbook

**Files:**
- Modify: `docs/runbooks/daily-candles-store.md`
- Create: `tests/services/daily_candles/test_migration_round_trip.py`

- [ ] **Step 1: Write the failing round-trip test**

```python
"""ROB-284 — full migration round-trip must preserve row count."""

from __future__ import annotations

import subprocess

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.slow
@pytest.mark.asyncio
async def test_upgrade_downgrade_upgrade_preserves_rows(
    session: AsyncSession,
) -> None:
    before = (await session.execute(
        text("SELECT count(*) FROM crypto_candles_1d")
    )).scalar_one()

    subprocess.run(["uv", "run", "alembic", "downgrade", "-3"], check=True)
    subprocess.run(["uv", "run", "alembic", "upgrade", "head"], check=True)

    after = (await session.execute(
        text("SELECT count(*) FROM crypto_candles_1d")
    )).scalar_one()
    assert before == after
```

- [ ] **Step 2: Run, expect PASS (the downgrade migrations from Tasks 7–9 already exist and should work)**

```bash
uv run pytest tests/services/daily_candles/test_migration_round_trip.py -v -m slow
```

If FAIL: fix the downgrade migrations in Tasks 7–9 before proceeding.

- [ ] **Step 3: Update the rollback runbook**

Append to `docs/runbooks/daily-candles-store.md`:

```markdown
### Automated rollback via alembic downgrade

If the issue is detected before the backup table is dropped, the cleanest
rollback is to alembic-downgrade the three ROB-284 revisions:

```bash
uv run alembic downgrade <rev_b>  # downgrades through e → d → c → b
```

This reverses step 3, step 2, and step 1 of the in-place migration.
`crypto_instruments` is preserved (cheap, useful for re-running).

If alembic downgrade fails partway, fall back to the manual procedure
above using `crypto_candles_1d_pre_rob283`.
```

- [ ] **Step 4: Commit**

```bash
git add tests/services/daily_candles/test_migration_round_trip.py \
        docs/runbooks/daily-candles-store.md
git commit -m "test(rob-284): round-trip migration test + rollback runbook"
```

---

### Task 13: ROB-282 regression check + final audit

**Files:**
- No new files; verification + PR description only.

- [ ] **Step 1: Re-run the audit grep**

```bash
grep -rln "crypto_candles_1d" --include="*.py" app/
```

Expected output (unchanged from Task 1):
```
app/services/daily_candles/repository.py
```

If anything else appears, **stop and explain why** — the scope of this PR has expanded silently.

- [ ] **Step 2: Run the full screener regression suite**

```bash
uv run pytest tests -k "screener_snapshot or invest_crypto_screener" -q
```

Expected: all pass, with no test file modifications in this PR's diff (the ROB-282 contract should be unaffected by construction).

- [ ] **Step 3: Run the full candle suite end-to-end**

```bash
uv run pytest tests -k "crypto_candles or crypto_instruments or daily_candles" -v
```

Expected: all pass.

- [ ] **Step 4: Compose the PR description**

Include:
- Branch name + worktree path.
- Alembic revision IDs (a/b/c/d/e).
- Audit grep output (Step 1 above).
- Screener regression test result (Step 2 above).
- Round-trip downgrade verification.
- Confirmation that no Binance SDK / adapter / ledger / scalper code shipped.
- Confirmation that no `crypto_candles_1d_view` was created.
- Confirmation that `app/jobs/invest_crypto_screener_snapshots.py` is unchanged.

- [ ] **Step 5: Push and open PR**

```bash
git push -u origin feature/ROB-284-crypto-instruments-schema
gh pr create --title "feat(rob-284): crypto_instruments master + venue-aware candle schema" \
  --body "$(cat <<'EOF'
## Summary
- Adds crypto_instruments master table; candles now reference it by instrument_id FK
- New crypto_candles_1m Timescale hypertable (1-day chunks)
- crypto_candles_1d in-place migration: backfill instrument_id, drop legacy symbol/market columns, add OHLC/non-negative CHECKs, new PK (instrument_id, time)
- DailyCandlesRepository crypto path now resolves instrument_id at write time
- ROB-282 screener contract unaffected (audit confirmed)

## Pre-implementation audit
[paste grep output]

## Tests
[paste pytest summary]

## Round-trip
upgrade → downgrade → upgrade preserves row count: [PASS/FAIL]

## Out of scope (deferred to Children B/C)
- Binance SDK / public market data adapter
- Binance testnet execution adapter
- binance_testnet_order_ledger + service
- scalping state machine
- transport-layer host allowlist code

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Self-review checklist (run after writing the plan, before executing)

- [ ] Every task has explicit file paths.
- [ ] Every "Step 1" with code contains the actual code, not a description.
- [ ] No "TBD", "TODO", "implement later" — search the doc.
- [ ] Migration revision IDs are placeholders (`<rev_a>` etc.); plan tells engineer to replace at generation time.
- [ ] Audit task is Task 1 — if its invariant breaks, the PR halts before destructive work.
- [ ] Round-trip downgrade test exists (Task 12).
- [ ] No `crypto_candles_1d_view` anywhere in this plan.
- [ ] No modification to `app/jobs/invest_crypto_screener_snapshots.py` in this plan.
- [ ] No Binance SDK / adapter / ledger / scalper code in any task.
- [ ] No scheduler entries in any task.
- [ ] Final audit re-run in Task 13 closes the loop.
- [ ] All commit messages reference `rob-284`.
- [ ] Worktree path follows `~/auto_trader/.worktrees/ROB-284` convention from CLAUDE.md.

---

## Open items deferred from parent plan §5 to be locked during execution

These are the open items relevant to Child A, with leans from parent plan:

| # | Item | Lean | Lock during |
|---|------|------|-------------|
| 2 | SQLAlchemy mixin/base for 1d/1m | Shared mixin if both classes diverge little; independent classes if 1d and 1m diverge in optional columns. Decide after writing Task 5 + Task 10. | Task 10 |
| 8 | Pre-migration backup mechanism | `CREATE TABLE AS` (atomic, fast). Documented in Task 6. | Task 6 |
| 10 | `crypto_candles_1d_view` materialized vs plain | **Dropped entirely** — YAGNI per pre-implementation audit. | — (closed) |

Items #1, #3, #4, #5, #6, #7, #9 from parent plan §5 are scoped to Children B/C and not addressed here.
