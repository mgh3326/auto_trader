# ROB-534 Toss Symbol Master + Market Cap Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Populate KR/US symbol-master metadata from Toss Open API and derive daily market-cap rows as `shares_outstanding * last_price`.

**Architecture:** Store stable Toss master fields on `kr_symbol_universe` / `us_symbol_universe`; store date/source-specific market-cap values in `market_valuation_snapshots` with `source='toss_openapi'`. Keep existing KRX MST / KIS COD universe discovery intact: Toss only enriches already-known universe rows. All operator entrypoints are dry-run by default and require `--commit` for writes.

**Tech Stack:** Python 3.13, SQLAlchemy async ORM, Alembic, Pydantic, httpx mock transports in tests, uv, pytest.

---

## File Structure

- Modify: `app/models/kr_symbol_universe.py` - add Toss master columns for KR rows.
- Modify: `app/models/us_symbol_universe.py` - add Toss master columns for US rows and keep `is_common_stock`.
- Modify: `app/models/market_valuation_snapshot.py` - allow `source='toss_openapi'`.
- Create: `alembic/versions/20260612_rob534_toss_symbol_master_market_cap.py` - additive universe columns plus market valuation source constraint update.
- Create: `app/services/toss_symbol_master_service.py` - batch orchestration, dry-run summary, universe upserts, and market-cap payload construction.
- Create: `scripts/sync_toss_symbol_master.py` - operator CLI for dry-run/commit.
- Modify: `app/services/invest_view_model/screener_service.py` - prefer explicit Toss common-share/security metadata over KR name heuristics when available.
- Modify: `tests/conftest.py` - conditional schema drift columns for persistent test DBs.
- Create: `tests/test_toss_symbol_master_service.py` - service-level tests for batch matching, dry-run, commit, and market-cap payloads.
- Modify: `tests/services/brokers/toss/test_dto.py` - cover KR suspension detail parsing assumptions.
- Create: `tests/test_toss_symbol_master_cli.py` - CLI defaults and output tests.
- Create: `tests/test_toss_symbol_master_screener_filter.py` - cover explicit Toss common-share/security/suspension filter behavior.

## Design Decisions

- `market_cap` goes to `market_valuation_snapshots`, not universe tables.
- `source='toss_openapi'` is added to the existing valuation source check constraint.
- Universe rows keep both the legacy US `is_common_stock` and the Toss field `is_common_share`; the sync service fills `is_common_stock` from Toss only when the Toss value is known.
- No scheduler activation in this issue. The deliverable is a manual dry-run-first CLI plus tested service APIs.
- No live Toss calls in default tests. Tests use fake Toss clients.
- This is a `high_risk_change`: implementation must not be merged/deployed or used for production backfill until stronger-model/CTO review clears migration and operator runbook.

---

### Task 1: Add Schema and ORM Columns

**Files:**
- Modify: `app/models/kr_symbol_universe.py`
- Modify: `app/models/us_symbol_universe.py`
- Modify: `app/models/market_valuation_snapshot.py`
- Create: `alembic/versions/20260612_rob534_toss_symbol_master_market_cap.py`
- Modify: `tests/conftest.py`

- [x] **Step 1: Write failing model/source tests**

Add tests that inspect ORM columns and source constraint text.

```python
# tests/test_toss_symbol_master_schema.py
from sqlalchemy import inspect

from app.models.kr_symbol_universe import KRSymbolUniverse
from app.models.market_valuation_snapshot import MarketValuationSnapshot
from app.models.us_symbol_universe import USSymbolUniverse


def test_kr_symbol_universe_has_toss_master_columns() -> None:
    columns = inspect(KRSymbolUniverse).columns
    for name in (
        "security_type",
        "is_common_share",
        "listing_status",
        "list_date",
        "delist_date",
        "shares_outstanding",
        "leverage_factor",
        "krx_trading_suspended",
        "nxt_trading_suspended",
        "isin",
        "toss_master_updated_at",
    ):
        assert name in columns


def test_us_symbol_universe_has_toss_master_columns() -> None:
    columns = inspect(USSymbolUniverse).columns
    for name in (
        "security_type",
        "is_common_share",
        "listing_status",
        "list_date",
        "delist_date",
        "shares_outstanding",
        "leverage_factor",
        "isin",
        "toss_master_updated_at",
    ):
        assert name in columns


def test_market_valuation_snapshot_allows_toss_openapi_source() -> None:
    constraints = MarketValuationSnapshot.__table_args__
    source_constraints = [
        c for c in constraints if getattr(c, "name", "") == "ck_market_valuation_snapshots_source"
    ]
    assert len(source_constraints) == 1
    assert "toss_openapi" in str(source_constraints[0].sqltext)
```

- [x] **Step 2: Run failing tests**

Run:

```bash
uv run pytest tests/test_toss_symbol_master_schema.py -q
```

Expected: FAIL because columns and `toss_openapi` constraint are missing.

- [x] **Step 3: Update ORM models**

Add imports and columns.

```python
# app/models/kr_symbol_universe.py
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import TIMESTAMP, Boolean, Date, ForeignKey, Index, Integer, Numeric, String, func

security_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
is_common_share: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
listing_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
list_date: Mapped[date | None] = mapped_column(Date, nullable=True)
delist_date: Mapped[date | None] = mapped_column(Date, nullable=True)
shares_outstanding: Mapped[Decimal | None] = mapped_column(Numeric(30, 0), nullable=True)
leverage_factor: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
krx_trading_suspended: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
nxt_trading_suspended: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
isin: Mapped[str | None] = mapped_column(String(20), nullable=True)
toss_master_updated_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
```

```python
# app/models/us_symbol_universe.py
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import TIMESTAMP, Boolean, Date, ForeignKey, Index, Integer, Numeric, String, func, text

security_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
is_common_share: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
listing_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
list_date: Mapped[date | None] = mapped_column(Date, nullable=True)
delist_date: Mapped[date | None] = mapped_column(Date, nullable=True)
shares_outstanding: Mapped[Decimal | None] = mapped_column(Numeric(30, 0), nullable=True)
leverage_factor: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
isin: Mapped[str | None] = mapped_column(String(20), nullable=True)
toss_master_updated_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
```

```python
# app/models/market_valuation_snapshot.py
CheckConstraint(
    "source IN ('naver_finance', 'yahoo', 'toss_openapi')",
    name="ck_market_valuation_snapshots_source",
),
```

- [x] **Step 4: Add Alembic migration**

Create `alembic/versions/20260612_rob534_toss_symbol_master_market_cap.py`.

```python
"""ROB-534 add Toss symbol master fields and valuation source

Revision ID: 20260612_rob534
Revises: 20260611_rob516_rob512_merge
Create Date: 2026-06-12 12:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260612_rob534"
down_revision = "20260611_rob516_rob512_merge"
branch_labels = None
depends_on = None


def _add_common_columns(table: str, *, kr: bool) -> None:
    op.add_column(table, sa.Column("security_type", sa.String(length=20), nullable=True))
    op.add_column(table, sa.Column("is_common_share", sa.Boolean(), nullable=True))
    op.add_column(table, sa.Column("listing_status", sa.String(length=20), nullable=True))
    op.add_column(table, sa.Column("list_date", sa.Date(), nullable=True))
    op.add_column(table, sa.Column("delist_date", sa.Date(), nullable=True))
    op.add_column(table, sa.Column("shares_outstanding", sa.Numeric(30, 0), nullable=True))
    op.add_column(table, sa.Column("leverage_factor", sa.Numeric(12, 6), nullable=True))
    if kr:
        op.add_column(table, sa.Column("krx_trading_suspended", sa.Boolean(), nullable=True))
        op.add_column(table, sa.Column("nxt_trading_suspended", sa.Boolean(), nullable=True))
    op.add_column(table, sa.Column("isin", sa.String(length=20), nullable=True))
    op.add_column(table, sa.Column("toss_master_updated_at", sa.TIMESTAMP(timezone=True), nullable=True))


def _drop_common_columns(table: str, *, kr: bool) -> None:
    op.drop_column(table, "toss_master_updated_at")
    op.drop_column(table, "isin")
    if kr:
        op.drop_column(table, "nxt_trading_suspended")
        op.drop_column(table, "krx_trading_suspended")
    op.drop_column(table, "leverage_factor")
    op.drop_column(table, "shares_outstanding")
    op.drop_column(table, "delist_date")
    op.drop_column(table, "list_date")
    op.drop_column(table, "listing_status")
    op.drop_column(table, "is_common_share")
    op.drop_column(table, "security_type")


def upgrade() -> None:
    _add_common_columns("kr_symbol_universe", kr=True)
    _add_common_columns("us_symbol_universe", kr=False)
    op.drop_constraint("ck_market_valuation_snapshots_source", "market_valuation_snapshots", type_="check")
    op.create_check_constraint(
        "ck_market_valuation_snapshots_source",
        "market_valuation_snapshots",
        "source IN ('naver_finance', 'yahoo', 'toss_openapi')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_market_valuation_snapshots_source", "market_valuation_snapshots", type_="check")
    op.create_check_constraint(
        "ck_market_valuation_snapshots_source",
        "market_valuation_snapshots",
        "source IN ('naver_finance', 'yahoo')",
    )
    _drop_common_columns("us_symbol_universe", kr=False)
    _drop_common_columns("kr_symbol_universe", kr=True)
```

- [x] **Step 5: Patch persistent test DB drift**

Add conditional column checks in `tests/conftest.py` after the existing universe drift patches. Use `ADD COLUMN IF NOT EXISTS` for universe fields and leave the check constraint to fresh `create_all`; persistent DBs that already have the old constraint should be migrated locally before DB-backed tests.

- [x] **Step 6: Run migration/model checks**

Run:

```bash
uv run pytest tests/test_toss_symbol_master_schema.py -q
uv run alembic heads
```

Expected: tests PASS, single Alembic head is `20260612_rob534`.

- [x] **Step 7: Commit**

```bash
git add app/models/kr_symbol_universe.py app/models/us_symbol_universe.py app/models/market_valuation_snapshot.py alembic/versions/20260612_rob534_toss_symbol_master_market_cap.py tests/conftest.py tests/test_toss_symbol_master_schema.py
git commit -m "feat(ROB-534): add Toss symbol master schema"
```

---

### Task 2: Build Toss Symbol Master Service

**Files:**
- Create: `app/services/toss_symbol_master_service.py`
- Create: `tests/test_toss_symbol_master_service.py`

- [x] **Step 1: Write failing service tests**

Cover dry-run behavior, 200-symbol batching, KR/US updates, missing Toss rows, and market-cap payload construction.

```python
# tests/test_toss_symbol_master_service.py
from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from app.models.kr_symbol_universe import KRSymbolUniverse
from app.models.us_symbol_universe import USSymbolUniverse
from app.services.brokers.toss.dto import TossPrice, TossStockInfo
from app.services.toss_symbol_master_service import (
    TossSymbolMasterSyncRequest,
    sync_toss_symbol_master,
)


class FakeTossClient:
    def __init__(self) -> None:
        self.stock_batches: list[tuple[str, ...]] = []
        self.price_batches: list[tuple[str, ...]] = []

    async def stocks(self, symbols):
        self.stock_batches.append(tuple(symbols))
        out = []
        for symbol in symbols:
            if symbol == "MISSING":
                continue
            is_kr = symbol.isdigit()
            out.append(
                TossStockInfo(
                    symbol=symbol,
                    name="삼성전자" if is_kr else "Apple",
                    english_name="Samsung Electronics" if is_kr else "Apple Inc.",
                    isin_code="KR7005930003" if is_kr else "US0378331005",
                    market="KR" if is_kr else "US",
                    security_type="STOCK",
                    is_common_share=symbol != "005935",
                    status="ACTIVE",
                    currency="KRW" if is_kr else "USD",
                    list_date="1975-06-11" if is_kr else "1980-12-12",
                    delist_date=None,
                    shares_outstanding=Decimal("5846278608") if is_kr else Decimal("14687356000"),
                    leverage_factor=None,
                    korean_market_detail={
                        "krxTradingSuspended": False,
                        "nxtTradingSuspended": False,
                    }
                    if is_kr
                    else None,
                )
            )
        return out

    async def prices(self, symbols):
        self.price_batches.append(tuple(symbols))
        return [
            TossPrice(
                symbol=symbol,
                timestamp="2026-06-12T00:00:00Z",
                last_price=Decimal("70000") if symbol.isdigit() else Decimal("190"),
                currency="KRW" if symbol.isdigit() else "USD",
            )
            for symbol in symbols
            if symbol != "MISSING"
        ]


@pytest.mark.asyncio
async def test_sync_toss_symbol_master_dry_run_does_not_mutate(db_session) -> None:
    db_session.add(KRSymbolUniverse(symbol="005930", name="삼성전자", exchange="KOSPI", is_active=True))
    await db_session.commit()

    result = await sync_toss_symbol_master(
        db_session,
        client=FakeTossClient(),
        request=TossSymbolMasterSyncRequest(market="kr", symbols=("005930",), commit=False),
    )

    assert result.commit is False
    assert result.symbols_requested == 1
    assert result.stocks_matched == 1
    row = await db_session.get(KRSymbolUniverse, "005930")
    assert row.shares_outstanding is None


@pytest.mark.asyncio
async def test_sync_toss_symbol_master_commit_updates_master_and_market_cap(db_session) -> None:
    db_session.add(KRSymbolUniverse(symbol="005930", name="삼성전자", exchange="KOSPI", is_active=True))
    await db_session.commit()

    result = await sync_toss_symbol_master(
        db_session,
        client=FakeTossClient(),
        request=TossSymbolMasterSyncRequest(
            market="kr",
            symbols=("005930",),
            commit=True,
            snapshot_date=dt.date(2026, 6, 12),
        ),
    )

    row = await db_session.get(KRSymbolUniverse, "005930")
    assert row.security_type == "STOCK"
    assert row.is_common_share is True
    assert row.shares_outstanding == Decimal("5846278608")
    assert row.krx_trading_suspended is False
    assert result.market_cap_payloads == 1
    assert result.market_cap_nonnull == 1
```

- [x] **Step 2: Run tests to verify failure**

Run:

```bash
uv run pytest tests/test_toss_symbol_master_service.py -q
```

Expected: FAIL because `app.services.toss_symbol_master_service` does not exist.

- [x] **Step 3: Implement request/result dataclasses and helpers**

Create focused dataclasses.

```python
# app/services/toss_symbol_master_service.py
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Protocol

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.kr_symbol_universe import KRSymbolUniverse
from app.models.us_symbol_universe import USSymbolUniverse
from app.services.brokers.toss.dto import TossPrice, TossStockInfo
from app.services.market_valuation_snapshots.repository import (
    MarketValuationSnapshotsRepository,
    MarketValuationSnapshotUpsert,
)

TOSS_VALUATION_SOURCE = "toss_openapi"
_BATCH_SIZE = 200


class TossSymbolMasterClient(Protocol):
    async def stocks(self, symbols: list[str] | tuple[str, ...]) -> list[TossStockInfo]: ...
    async def prices(self, symbols: list[str] | tuple[str, ...]) -> list[TossPrice]: ...


@dataclass(frozen=True)
class TossSymbolMasterSyncRequest:
    market: str
    symbols: tuple[str, ...] = ()
    all_symbols: bool = False
    limit: int | None = 20
    commit: bool = False
    include_market_cap: bool = True
    snapshot_date: dt.date | None = None


@dataclass(frozen=True)
class TossSymbolMasterSyncResult:
    market: str
    commit: bool
    symbols_requested: int
    batches: int
    stocks_matched: int
    stocks_missing: int
    master_updates: int
    market_cap_payloads: int
    market_cap_nonnull: int
    warnings: tuple[str, ...] = ()
    samples: tuple[str, ...] = ()


def _chunks(symbols: list[str], size: int = _BATCH_SIZE) -> list[list[str]]:
    return [symbols[idx : idx + size] for idx in range(0, len(symbols), size)]
```

- [x] **Step 4: Implement symbol resolution**

Resolve explicit symbols or active universe symbols.

```python
async def _resolve_symbols(
    db: AsyncSession,
    *,
    market: str,
    symbols: tuple[str, ...],
    all_symbols: bool,
    limit: int | None,
) -> list[str]:
    if market not in {"kr", "us"}:
        raise ValueError(f"unsupported market: {market}")
    if symbols:
        return [s.strip().upper() for s in symbols if s.strip()]
    model = KRSymbolUniverse if market == "kr" else USSymbolUniverse
    stmt = sa.select(model.symbol).where(model.is_active.is_(True)).order_by(model.symbol)
    if not all_symbols:
        stmt = stmt.limit(limit or 20)
    result = await db.execute(stmt)
    return [row[0] for row in result.all()]
```

- [x] **Step 5: Implement master field application**

Map Toss rows onto existing universe rows only.

```python
def _parse_date(value: str | None) -> dt.date | None:
    if not value:
        return None
    return dt.date.fromisoformat(value[:10])


def _kr_suspended(detail: dict | None, key: str) -> bool | None:
    if not detail:
        return None
    if key in detail:
        return bool(detail[key])
    return None


def _kr_assignments(stock: TossStockInfo, *, now: dt.datetime) -> dict[str, object]:
    return {
        "security_type": stock.security_type,
        "is_common_share": stock.is_common_share,
        "listing_status": stock.status,
        "list_date": _parse_date(stock.list_date),
        "delist_date": _parse_date(stock.delist_date),
        "shares_outstanding": stock.shares_outstanding,
        "leverage_factor": stock.leverage_factor,
        "krx_trading_suspended": _kr_suspended(stock.korean_market_detail, "krxTradingSuspended"),
        "nxt_trading_suspended": _kr_suspended(stock.korean_market_detail, "nxtTradingSuspended"),
        "isin": stock.isin_code,
        "toss_master_updated_at": now,
    }


def _us_assignments(stock: TossStockInfo, *, now: dt.datetime) -> dict[str, object]:
    return {
        "security_type": stock.security_type,
        "is_common_share": stock.is_common_share,
        "listing_status": stock.status,
        "list_date": _parse_date(stock.list_date),
        "delist_date": _parse_date(stock.delist_date),
        "shares_outstanding": stock.shares_outstanding,
        "leverage_factor": stock.leverage_factor,
        "isin": stock.isin_code,
        "toss_master_updated_at": now,
        "is_common_stock": stock.is_common_share,
    }


def _count_or_apply(row: object, assignments: dict[str, object], *, apply: bool) -> bool:
    changed = False
    for field, value in assignments.items():
        if getattr(row, field) != value:
            if apply:
                setattr(row, field, value)
            changed = True
    return changed
```

- [x] **Step 6: Implement market-cap payloads and sync orchestrator**

Compute market cap only when both shares and last price are available.

```python
def _market_cap_payloads(
    *,
    market: str,
    snapshot_date: dt.date,
    stocks: dict[str, TossStockInfo],
    prices: dict[str, TossPrice],
) -> list[MarketValuationSnapshotUpsert]:
    payloads: list[MarketValuationSnapshotUpsert] = []
    for symbol, stock in stocks.items():
        price = prices.get(symbol)
        market_cap = (
            stock.shares_outstanding * price.last_price
            if price is not None and stock.shares_outstanding is not None
            else None
        )
        if market_cap is None:
            continue
        payloads.append(
            MarketValuationSnapshotUpsert(
                market=market,
                symbol=symbol,
                snapshot_date=snapshot_date,
                source=TOSS_VALUATION_SOURCE,
                market_cap=market_cap,
                raw_payload={
                    "source": TOSS_VALUATION_SOURCE,
                    "sharesOutstanding": str(stock.shares_outstanding),
                    "lastPrice": str(price.last_price),
                    "priceTimestamp": price.timestamp,
                    "currency": price.currency,
                },
            )
        )
    return payloads
```

```python
async def sync_toss_symbol_master(
    db: AsyncSession,
    *,
    client: TossSymbolMasterClient,
    request: TossSymbolMasterSyncRequest,
) -> TossSymbolMasterSyncResult:
    market = request.market.strip().lower()
    snapshot_date = request.snapshot_date or dt.datetime.now(dt.UTC).date()
    symbols = await _resolve_symbols(
        db,
        market=market,
        symbols=request.symbols,
        all_symbols=request.all_symbols,
        limit=request.limit,
    )
    if not symbols:
        return TossSymbolMasterSyncResult(
            market=market,
            commit=request.commit,
            symbols_requested=0,
            batches=0,
            stocks_matched=0,
            stocks_missing=0,
            master_updates=0,
            market_cap_payloads=0,
            market_cap_nonnull=0,
            warnings=("no symbols resolved",),
        )

    model = KRSymbolUniverse if market == "kr" else USSymbolUniverse
    existing_result = await db.execute(sa.select(model).where(model.symbol.in_(symbols)))
    existing = {row.symbol: row for row in existing_result.scalars().all()}
    now = dt.datetime.now(dt.UTC)
    all_stocks: dict[str, TossStockInfo] = {}
    all_prices: dict[str, TossPrice] = {}
    updates = 0

    batches = _chunks(symbols)
    for batch in batches:
        stock_rows = {row.symbol: row for row in await client.stocks(batch)}
        price_rows = {row.symbol: row for row in await client.prices(batch)} if request.include_market_cap else {}
        all_stocks.update(stock_rows)
        all_prices.update(price_rows)
        for symbol, stock in stock_rows.items():
            row = existing.get(symbol)
            if row is None:
                continue
            assignments = (
                _kr_assignments(stock, now=now)
                if market == "kr"
                else _us_assignments(stock, now=now)
            )
            changed = _count_or_apply(row, assignments, apply=request.commit)
            if changed:
                updates += 1

    payloads = _market_cap_payloads(
        market=market,
        snapshot_date=snapshot_date,
        stocks=all_stocks,
        prices=all_prices,
    )
    if request.commit:
        if payloads:
            await MarketValuationSnapshotsRepository(db).upsert(payloads)
        await db.flush()

    missing = len(set(symbols) - set(all_stocks))
    return TossSymbolMasterSyncResult(
        market=market,
        commit=request.commit,
        symbols_requested=len(symbols),
        batches=len(batches),
        stocks_matched=len(all_stocks),
        stocks_missing=missing,
        master_updates=updates,
        market_cap_payloads=len(payloads),
        market_cap_nonnull=len(payloads),
        samples=tuple(symbols[:10]),
    )
```

- [x] **Step 7: Run service tests**

Run:

```bash
uv run pytest tests/test_toss_symbol_master_service.py -q
```

Expected: PASS.

- [x] **Step 8: Commit**

```bash
git add app/services/toss_symbol_master_service.py tests/test_toss_symbol_master_service.py
git commit -m "feat(ROB-534): sync Toss symbol master metadata"
```

---

### Task 3: Add Dry-Run-First CLI

**Files:**
- Create: `scripts/sync_toss_symbol_master.py`
- Create: `tests/test_toss_symbol_master_cli.py`

- [x] **Step 1: Write failing CLI parser tests**

```python
# tests/test_toss_symbol_master_cli.py
from scripts.sync_toss_symbol_master import parse_args


def test_parse_args_defaults_to_dry_run() -> None:
    args = parse_args(["--market", "kr", "--symbol", "005930"])
    assert args.market == "kr"
    assert args.symbol == ["005930"]
    assert args.commit is False


def test_parse_args_all_excludes_symbol_and_limit() -> None:
    try:
        parse_args(["--market", "kr", "--all", "--symbol", "005930"])
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("expected parser error")
```

- [x] **Step 2: Run failing CLI tests**

Run:

```bash
uv run pytest tests/test_toss_symbol_master_cli.py -q
```

Expected: FAIL because script does not exist.

- [x] **Step 3: Implement CLI**

```python
#!/usr/bin/env python3
"""Sync Toss Open API symbol master metadata and market-cap valuation rows.

Defaults to dry-run. Pass --commit only after reviewing the printed coverage packet.
"""

from __future__ import annotations

import argparse
import asyncio


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Dry-run-first Toss symbol master sync (ROB-534).")
    parser.add_argument("--market", choices=["kr", "us"], required=True)
    parser.add_argument("--symbol", action="append", default=[], help="Restrict to one symbol. Can be repeated.")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--all", action="store_true", help="Process all active universe symbols.")
    parser.add_argument("--no-market-cap", action="store_true", help="Update master fields only; skip prices/market cap.")
    parser.add_argument("--commit", action="store_true", help="Write changes. Default is dry-run.")
    args = parser.parse_args(argv)
    if args.all and (args.symbol or args.limit != 20):
        parser.error("--all is mutually exclusive with --symbol and explicit --limit")
    if args.limit < 1:
        parser.error("--limit must be >= 1")
    return args


def _print_result(result) -> None:
    print(
        f"\nToss symbol master {result.market.upper()} "
        f"(dry_run={not result.commit}, batches={result.batches})"
    )
    print("coverage:")
    print(f"  requested: {result.symbols_requested}")
    print(f"  stocks_matched: {result.stocks_matched}")
    print(f"  stocks_missing: {result.stocks_missing}")
    print(f"  master_updates: {result.master_updates}")
    print(f"  market_cap_payloads: {result.market_cap_payloads}")
    print(f"  market_cap_nonnull: {result.market_cap_nonnull}")
    if result.samples:
        print("samples:")
        for sample in result.samples:
            print(f"  {sample}")
    if not result.commit:
        print("\n--dry-run: no rows written.\n")
    else:
        print("\ncommitted Toss symbol master updates.\n")


async def run(args: argparse.Namespace) -> int:
    from app.core.db import AsyncSessionLocal
    from app.services.brokers.toss.client import TossReadClient
    from app.services.toss_symbol_master_service import (
        TossSymbolMasterSyncRequest,
        sync_toss_symbol_master,
    )

    client = TossReadClient.from_settings()
    try:
        async with AsyncSessionLocal() as session:
            async with session.begin():
                result = await sync_toss_symbol_master(
                    session,
                    client=client,
                    request=TossSymbolMasterSyncRequest(
                        market=args.market,
                        symbols=tuple(args.symbol),
                        all_symbols=args.all,
                        limit=args.limit,
                        commit=args.commit,
                        include_market_cap=not args.no_market_cap,
                    ),
                )
            _print_result(result)
    finally:
        await client.aclose()
    return 0


async def main() -> int:
    args = parse_args()
    from app.core.cli import setup_logging_and_sentry

    setup_logging_and_sentry(service_name="sync-toss-symbol-master")
    return await run(args)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
```

- [x] **Step 4: Run CLI tests**

Run:

```bash
uv run pytest tests/test_toss_symbol_master_cli.py -q
```

Expected: PASS.

- [x] **Step 5: Commit**

```bash
git add scripts/sync_toss_symbol_master.py tests/test_toss_symbol_master_cli.py
git commit -m "feat(ROB-534): add Toss symbol master sync CLI"
```

---

### Task 4: Use Toss Master Fields in Screener Filters

**Files:**
- Modify: `app/services/invest_view_model/screener_service.py`
- Create: `tests/test_toss_symbol_master_screener_filter.py`

- [x] **Step 1: Write failing filter tests**

Cover that explicit Toss fields exclude preferred shares and ETFs before name heuristics.

```python
# tests/test_toss_symbol_master_screener_filter.py
from app.services.invest_view_model.screener_service import _is_toss_common_stock_row


def test_toss_common_stock_row_prefers_explicit_false() -> None:
    assert (
        _is_toss_common_stock_row(
            symbol="005935",
            name="삼성전자우",
            security_type="STOCK",
            is_common_share=False,
            trading_suspended=False,
        )
        is False
    )


def test_toss_common_stock_row_excludes_etf_even_when_common_unknown() -> None:
    assert (
        _is_toss_common_stock_row(
            symbol="069500",
            name="KODEX 200",
            security_type="ETF",
            is_common_share=None,
            trading_suspended=False,
        )
        is False
    )


def test_toss_common_stock_row_falls_back_to_name_heuristic() -> None:
    assert (
        _is_toss_common_stock_row(
            symbol="005930",
            name="삼성전자",
            security_type=None,
            is_common_share=None,
            trading_suspended=None,
        )
        is True
    )
```

- [x] **Step 2: Run failing filter tests**

Run:

```bash
uv run pytest tests/test_toss_symbol_master_screener_filter.py -q
```

Expected: FAIL because `_is_toss_common_stock_row` does not exist.

- [x] **Step 3: Implement helper**

In `app/services/invest_view_model/screener_service.py`, add a helper next to `_is_kr_toss_common_stock`.

```python
def _is_toss_common_stock_row(
    *,
    symbol: str,
    name: str | None,
    security_type: str | None,
    is_common_share: bool | None,
    trading_suspended: bool | None,
) -> bool:
    if trading_suspended is True:
        return False
    if security_type is not None and security_type.upper() not in {"STOCK", "REIT"}:
        return False
    if is_common_share is False:
        return False
    if is_common_share is True:
        return True
    return _is_kr_toss_common_stock(symbol, name)
```

- [x] **Step 4: Replace KR query use sites**

Where KR screener loaders currently select `KRSymbolUniverse.symbol`, `name`, and sector fields, include:

```python
KRSymbolUniverse.security_type,
KRSymbolUniverse.is_common_share,
KRSymbolUniverse.krx_trading_suspended,
KRSymbolUniverse.nxt_trading_suspended,
```

Build metadata maps and replace:

```python
if market == "kr" and not _is_kr_toss_common_stock(snap.symbol, symbol_names.get(snap.symbol)):
    continue
```

with:

```python
meta = symbol_meta.get(snap.symbol, {})
if market == "kr" and not _is_toss_common_stock_row(
    symbol=snap.symbol,
    name=symbol_names.get(snap.symbol),
    security_type=meta.get("security_type"),
    is_common_share=meta.get("is_common_share"),
    trading_suspended=meta.get("krx_trading_suspended") or meta.get("nxt_trading_suspended"),
):
    continue
```

Keep fallback behavior when metadata columns are null.

- [x] **Step 5: Run focused screener tests**

Run:

```bash
uv run pytest tests/test_toss_symbol_master_screener_filter.py tests/test_invest_common_preferred_disparity.py -q
```

Expected: PASS.

- [x] **Step 6: Commit**

```bash
git add app/services/invest_view_model/screener_service.py tests/test_toss_symbol_master_screener_filter.py tests/test_invest_common_preferred_disparity.py
git commit -m "feat(ROB-534): use Toss master fields in screener filters"
```

---

### Task 5: Verify Valuation Consumption Path

**Files:**
- Modify: `tests/test_invest_coverage_valuation.py`
- Modify: `tests/services/market_valuation_snapshots/test_repository_latest_for_symbols.py`

- [x] **Step 1: Write failing test for `toss_openapi` valuation row**

Ensure repository upsert and latest partition consumers accept `source='toss_openapi'`.

```python
import datetime as dt
from decimal import Decimal

import pytest

from app.services.market_valuation_snapshots.repository import (
    MarketValuationSnapshotsRepository,
    MarketValuationSnapshotUpsert,
)


@pytest.mark.asyncio
async def test_market_valuation_accepts_toss_openapi_market_cap(db_session) -> None:
    repo = MarketValuationSnapshotsRepository(db_session)
    await repo.upsert(
        [
            MarketValuationSnapshotUpsert(
                market="kr",
                symbol="005930",
                snapshot_date=dt.date(2026, 6, 12),
                source="toss_openapi",
                market_cap=Decimal("409239502560000"),
                raw_payload={"source": "toss_openapi"},
            )
        ]
    )
    await db_session.commit()

    rows = await repo.latest_for_symbols(market="kr", symbols={"005930"})
    assert rows[0].source == "toss_openapi"
    assert rows[0].market_cap == Decimal("409239502560000")
```

- [x] **Step 2: Run valuation tests**

Run:

```bash
uv run pytest tests/test_invest_coverage_valuation.py tests/services/market_valuation_snapshots/test_repository_latest_for_symbols.py -q
```

Expected: PASS after Task 1 source constraint change.

- [x] **Step 3: Search for source allowlists and update explicit matches**

Run:

```bash
rg -n "naver_finance.*yahoo|yahoo.*naver_finance|ck_market_valuation_snapshots_source|source.*market_valuation" app tests
```

Expected: only the ORM check constraint, Alembic migration, and tests require changes. If another explicit valuation source allowlist is found, add `toss_openapi` and extend `test_market_valuation_accepts_toss_openapi_market_cap` to exercise that path.

- [x] **Step 4: Commit**

```bash
git add tests/test_invest_coverage_valuation.py tests/services/market_valuation_snapshots/test_repository_latest_for_symbols.py
git commit -m "test(ROB-534): cover Toss market cap valuation rows"
```

---

### Task 6: Documentation and Operator Runbook

**Files:**
- Modify: `app/mcp_server/README.md` only if MCP-facing screener behavior is described there.
- Create or modify: `docs/runbooks/toss-symbol-master-sync.md`
- Modify: `docs/invest/data-source-contract.md` if source authority needs updating.

- [x] **Step 1: Add runbook**

Create `docs/runbooks/toss-symbol-master-sync.md`.

```markdown
# Toss Symbol Master Sync Runbook

ROB-534 adds a dry-run-first sync for Toss Open API symbol master metadata and market-cap valuation rows.

## Dry Run

~~~bash
uv run python -m scripts.sync_toss_symbol_master --market kr --limit 20
uv run python -m scripts.sync_toss_symbol_master --market us --limit 20
~~~

Expected output includes requested symbols, matched stocks, missing stocks, master updates, and market-cap payload count. Dry-run writes no rows.

## Commit

Run only after reviewing dry-run coverage and after ROB-534 stronger-model/CTO migration review clears the change.

~~~bash
uv run python -m scripts.sync_toss_symbol_master --market kr --all --commit
uv run python -m scripts.sync_toss_symbol_master --market us --all --commit
~~~

## Rollback

This migration is additive for universe fields. To remove Toss-derived market-cap rows for one date:

~~~sql
DELETE FROM market_valuation_snapshots
WHERE source = 'toss_openapi'
  AND snapshot_date = DATE 'YYYY-MM-DD';
~~~

Do not delete existing `naver_finance` or `yahoo` valuation rows.
```

- [x] **Step 2: Update data source contract**

In `docs/invest/data-source-contract.md`, document:

- universe master enrichment source: `toss_openapi`
- market-cap source: `market_valuation_snapshots.source=toss_openapi`
- Toss is production API source for this read-model only, not frontend scraping.

- [x] **Step 3: Run docs grep check**

Run:

```bash
rg -n "toss_openapi|sync_toss_symbol_master|Toss Symbol Master" docs app/mcp_server/README.md
```

Expected: runbook and source contract references are present.

- [x] **Step 4: Commit**

```bash
git add docs/runbooks/toss-symbol-master-sync.md docs/invest/data-source-contract.md app/mcp_server/README.md
git commit -m "docs(ROB-534): add Toss symbol master sync runbook"
```

---

### Task 7: Final Verification and Review Hold

**Files:**
- No code files unless verification finds a defect.

- [x] **Step 1: Run focused tests**

Run:

```bash
uv run pytest \
  tests/test_toss_symbol_master_schema.py \
  tests/test_toss_symbol_master_service.py \
  tests/test_toss_symbol_master_cli.py \
  tests/test_toss_symbol_master_screener_filter.py \
  tests/services/brokers/toss/test_client.py \
  tests/services/brokers/toss/test_dto.py \
  tests/services/market_valuation_snapshots/test_repository_latest_for_symbols.py \
  -q
```

Expected: PASS.

- [x] **Step 2: Run lint**

Run:

```bash
make lint
```

Expected: PASS.

- [x] **Step 3: Run Alembic sanity**

Run:

```bash
uv run alembic heads
```

Expected: one head, `20260612_rob534`.

- [x] **Step 4: Run dry-run smoke if Toss credentials are configured**

Run only when `TOSS_API_ENABLED=true` and credentials are present:

```bash
uv run python -m scripts.sync_toss_symbol_master --market kr --symbol 005930
uv run python -m scripts.sync_toss_symbol_master --market us --symbol AAPL
```

Expected: dry-run coverage shows one requested symbol, one matched stock, one market-cap payload, no writes.

- [x] **Step 5: Add Linear hold comment**

Add a ROB-534 comment:

```markdown
Implementation is ready for ROB-534, but I am applying hold_for_final_review because this includes an Alembic migration and production data-source/backfill behavior. No merge, production migration, or full-universe `--commit` run until stronger-model/CTO review clears migration safety, rollback, and operator runbook assumptions.
```

- [x] **Step 6: Apply Linear `hold_for_final_review` label**

Use Linear label `hold_for_final_review` after implementation is complete and tests pass.

- [x] **Step 7: Commit verification notes if any docs changed**

```bash
git status --short
```

Expected: clean or only intended verification artifacts.

---

## Self-Review

- Spec coverage: Covers additive universe columns, dry-run/commit sync, Toss 200-symbol batches, market-cap valuation rows, US common-stock fill, screener ETF/preferred/suspension guard, migration, and operator runbook.
- Scope intentionally excluded: scheduler activation, live order/trading behavior, Toss warnings, Toss candles, exchange-rate replacement.
- Type consistency: `is_common_share` is the Toss field; `is_common_stock` remains the legacy US classifier field. `source='toss_openapi'` is valuation-only.
- Review gate: Because this is a DB migration and production data backfill path, merge/deploy/full commit requires `needs_stronger_model_review` clearance.
