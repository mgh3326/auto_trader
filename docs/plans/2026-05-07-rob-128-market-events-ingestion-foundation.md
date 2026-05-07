# ROB-128: Market Events Ingestion Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a PR-sized foundation that lets auto_trader ingest, store, and read "market events" (US earnings via Finnhub one-day partitions, KR DART disclosures, and a forward-compatible crypto event taxonomy) so a later Prefect deployment can drive it.

**Architecture:** Three new public-schema tables (`market_events`, `market_event_values`, `market_event_ingestion_partitions`) hold the event ledger and per-day ingestion state. Pure-function normalizers translate Finnhub / DART responses to dicts; an async repository performs PostgreSQL `INSERT ... ON CONFLICT DO UPDATE` upserts keyed by partial unique indexes. A per-day ingestor records partition state (running/succeeded/failed) so failed dates can be retried without holes. A CLI splits date ranges into one-day partitions for later Prefect to call. A read-only service + FastAPI router exposes events by date range with `held`/`watched` placeholder flags.

**Tech Stack:** Python 3.13, SQLAlchemy 2 async ORM, Alembic, FastAPI, Pydantic v2, finnhub-python, OpenDartReader, pytest + pytest-asyncio.

---

## Pre-flight context for the implementer

You are working in the existing `auto_trader` repo. **Read this section before starting Task 1** — the rest of the plan assumes you understand it.

### Existing patterns you must follow

| Concern | Reference file | What to copy |
| --- | --- | --- |
| Declarative `Base` | `app/models/base.py:15` | `from app.models.base import Base` |
| Public-schema model w/ JSONB + unique + check constraints | `app/models/manual_holdings.py:115-159` | `__tablename__`, `UniqueConstraint`, `server_default=func.now()`, `onupdate=func.now()` |
| Partial unique indexes (`Index(..., unique=True, postgresql_where=text(...))`) | `app/models/review.py:264-279` | conditional uniques on `source_event_id IS NOT NULL` |
| Alembic migration template | `alembic/versions/c1d2e3f4a5b6_add_alpaca_paper_order_ledger.py` | `op.create_table` + `op.create_index` + `downgrade()` |
| Service class pattern (AsyncSession injected) | `app/services/alpaca_paper_ledger_service.py:1-30` | `class XxxService: def __init__(self, db: AsyncSession)` + `pg_insert(...).on_conflict_do_update(...)` |
| Read-only router | `app/routers/alpaca_paper_ledger.py:1-50` | `Depends(get_db)` + `Depends(get_authenticated_user)` + Pydantic response_model |
| Router registration | `app/main.py:192` (`alpaca_paper_ledger.router`) | `app.include_router(market_events.router)` after the alpaca line |
| Async DB test fixture | `tests/conftest.py:406-421` (`db_session`) | depends on real Postgres at `DATABASE_URL`; auto-creates schemas + tables |
| Service unit test (DB-backed) | `tests/services/test_alpaca_paper_ledger_service.py` | `@pytest.mark.unit` + `db_session` fixture |
| CLI script template | `scripts/sync_us_symbol_universe.py:1-38` | `asyncio.run(main())`, `init_sentry(service_name=...)`, `capture_exception(...)` |
| Runbook doc style | `docs/runbooks/alpaca-paper-ledger.md` | overview / commands / safety / follow-ups sections |

### Existing integration points

* **Finnhub earnings calendar fetch:** `app/mcp_server/tooling/fundamentals_sources_finnhub.py:175-231` (`_fetch_earnings_calendar_finnhub`). It already returns the normalized list shape we want to ingest. Reuse it via import — do not duplicate the Finnhub client wiring.
* **DART filings fetch:** `app/services/disclosures/dart.py:220-267` (`list_filings`). Per-symbol only. For market-wide per-day fetch we need a thin new helper that calls `OpenDartReader.list_date(date)`; the OpenDartReader instance is already memoized via `_get_client()` (line 155).
* **Settings / env:** `FINNHUB_API_KEY` and `OPENDART_API_KEY` are already in `app/core/config.py`. Tests stub both in `tests/conftest.py:48-93` with `DUMMY_*` values. **Do not add new env vars.**
* **Alembic head:** `2026_05_06_rob119`. Set this as `down_revision` of the new migration.

### Files this plan creates

```
app/models/market_events.py
app/schemas/market_events.py
app/services/market_events/__init__.py
app/services/market_events/taxonomy.py
app/services/market_events/normalizers.py
app/services/market_events/repository.py
app/services/market_events/ingestion.py
app/services/market_events/query_service.py
app/services/market_events/dart_helpers.py
app/routers/market_events.py
scripts/ingest_market_events.py
alembic/versions/<rev>_add_market_events_tables.py
docs/runbooks/market-events-ingestion.md
tests/services/test_market_events_models.py
tests/services/test_market_events_normalizers.py
tests/services/test_market_events_repository.py
tests/services/test_market_events_ingestion.py
tests/services/test_market_events_query_service.py
tests/test_market_events_router.py
tests/test_market_events_cli.py
```

### Files this plan modifies

```
app/main.py            # register router (one line)
CLAUDE.md              # add ROB-128 section
```

### Working rules

* All writes go through the repository / service layer — **no `INSERT`/`UPDATE`/`DELETE` SQL outside `app/services/market_events/repository.py`**.
* `raw_payload_json` columns must redact API keys / secrets before write. Reuse `_redact_sensitive_keys` from `app/services/alpaca_paper_ledger_service.py:106-119` (import it; do not copy).
* No broker / order / watch / trade-journal mutation. The CLI runs offline.
* Tests must not require live Finnhub / DART credentials. Mock both via `monkeypatch.setattr` against the imported helper symbols.
* Commit after every task.

---

## Task 1: Add `MarketEvent`, `MarketEventValue`, `MarketEventIngestionPartition` ORM models

**Files:**
- Create: `app/models/market_events.py`
- Test: `tests/services/test_market_events_models.py`

- [ ] **Step 1.1: Write failing model-shape test**

Create `tests/services/test_market_events_models.py`:

```python
"""ORM shape and constraint tests for market_events tables (ROB-128)."""

from __future__ import annotations

import pytest


@pytest.mark.unit
def test_market_event_model_columns():
    from app.models.market_events import MarketEvent

    cols = {c.name for c in MarketEvent.__table__.columns}
    expected = {
        "id",
        "event_uuid",
        "category",
        "market",
        "country",
        "symbol",
        "company_name",
        "title",
        "event_date",
        "release_time_utc",
        "release_time_local",
        "source_timezone",
        "time_hint",
        "importance",
        "status",
        "source",
        "source_event_id",
        "source_url",
        "fiscal_year",
        "fiscal_quarter",
        "raw_payload_json",
        "fetched_at",
        "created_at",
        "updated_at",
    }
    assert expected <= cols
    assert MarketEvent.__table__.schema is None  # public schema


@pytest.mark.unit
def test_market_event_partial_unique_indexes_exist():
    from app.models.market_events import MarketEvent

    index_names = {idx.name for idx in MarketEvent.__table__.indexes}
    assert "uq_market_events_source_event_id" in index_names
    assert "uq_market_events_natural_key" in index_names


@pytest.mark.unit
def test_market_event_value_model_columns():
    from app.models.market_events import MarketEventValue

    cols = {c.name for c in MarketEventValue.__table__.columns}
    expected = {
        "id",
        "event_id",
        "metric_name",
        "period",
        "actual",
        "forecast",
        "previous",
        "revised_previous",
        "unit",
        "surprise",
        "surprise_pct",
        "released_at",
        "created_at",
        "updated_at",
    }
    assert expected <= cols


@pytest.mark.unit
def test_market_event_ingestion_partition_model_columns():
    from app.models.market_events import MarketEventIngestionPartition

    cols = {c.name for c in MarketEventIngestionPartition.__table__.columns}
    expected = {
        "id",
        "source",
        "category",
        "market",
        "partition_date",
        "status",
        "event_count",
        "started_at",
        "finished_at",
        "last_error",
        "retry_count",
        "source_request_hash",
        "created_at",
        "updated_at",
    }
    assert expected <= cols

    constraint_names = {c.name for c in MarketEventIngestionPartition.__table__.constraints}
    assert "uq_market_event_ingestion_partitions_source" in constraint_names
```

- [ ] **Step 1.2: Run test to confirm it fails**

Run: `uv run pytest tests/services/test_market_events_models.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.models.market_events'`.

- [ ] **Step 1.3: Implement the models**

Create `app/models/market_events.py`:

```python
"""Market events foundation models (ROB-128).

Stores ingested market-wide events (US earnings via Finnhub, KR DART disclosures,
crypto exchange notices, etc.) plus per-day ingestion state for retryable partitions.

All writes must go through MarketEventsRepository. No direct SQL writes.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    Date,
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


class MarketEvent(Base):
    __tablename__ = "market_events"
    __table_args__ = (
        Index(
            "uq_market_events_source_event_id",
            "source",
            "category",
            "market",
            "source_event_id",
            unique=True,
            postgresql_where=text("source_event_id IS NOT NULL"),
        ),
        Index(
            "uq_market_events_natural_key",
            "source",
            "category",
            "market",
            "symbol",
            "event_date",
            "fiscal_year",
            "fiscal_quarter",
            unique=True,
            postgresql_where=text("source_event_id IS NULL"),
        ),
        Index("ix_market_events_event_date", "event_date"),
        Index("ix_market_events_symbol", "symbol"),
        Index("ix_market_events_category_market_date", "category", "market", "event_date"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    event_uuid: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=False,
        unique=True,
        server_default=text("gen_random_uuid()"),
    )

    category: Mapped[str] = mapped_column(Text, nullable=False)
    market: Mapped[str] = mapped_column(Text, nullable=False)
    country: Mapped[str | None] = mapped_column(Text)
    symbol: Mapped[str | None] = mapped_column(Text)
    company_name: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)

    event_date: Mapped[date] = mapped_column(Date, nullable=False)
    release_time_utc: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    release_time_local: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=False))
    source_timezone: Mapped[str | None] = mapped_column(Text)
    time_hint: Mapped[str | None] = mapped_column(Text)

    importance: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="scheduled")

    source: Mapped[str] = mapped_column(Text, nullable=False)
    source_event_id: Mapped[str | None] = mapped_column(Text)
    source_url: Mapped[str | None] = mapped_column(Text)

    fiscal_year: Mapped[int | None] = mapped_column(Integer)
    fiscal_quarter: Mapped[int | None] = mapped_column(Integer)

    raw_payload_json: Mapped[dict | None] = mapped_column(JSONB)

    fetched_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class MarketEventValue(Base):
    __tablename__ = "market_event_values"
    __table_args__ = (
        UniqueConstraint(
            "event_id",
            "metric_name",
            "period",
            name="uq_market_event_values_event_metric_period",
        ),
        Index("ix_market_event_values_event_id", "event_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    event_id: Mapped[int] = mapped_column(
        ForeignKey("market_events.id", ondelete="CASCADE"), nullable=False
    )
    metric_name: Mapped[str] = mapped_column(Text, nullable=False)
    period: Mapped[str | None] = mapped_column(Text)

    actual: Mapped[float | None] = mapped_column(Numeric(28, 8))
    forecast: Mapped[float | None] = mapped_column(Numeric(28, 8))
    previous: Mapped[float | None] = mapped_column(Numeric(28, 8))
    revised_previous: Mapped[float | None] = mapped_column(Numeric(28, 8))
    unit: Mapped[str | None] = mapped_column(Text)
    surprise: Mapped[float | None] = mapped_column(Numeric(28, 8))
    surprise_pct: Mapped[float | None] = mapped_column(Numeric(12, 4))

    released_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class MarketEventIngestionPartition(Base):
    __tablename__ = "market_event_ingestion_partitions"
    __table_args__ = (
        UniqueConstraint(
            "source",
            "category",
            "market",
            "partition_date",
            name="uq_market_event_ingestion_partitions_source",
        ),
        Index(
            "ix_market_event_ingestion_partitions_status_date",
            "status",
            "partition_date",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(Text, nullable=False)
    market: Mapped[str] = mapped_column(Text, nullable=False)
    partition_date: Mapped[date] = mapped_column(Date, nullable=False)

    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="pending")
    event_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    started_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    source_request_hash: Mapped[str | None] = mapped_column(Text)

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

- [ ] **Step 1.4: Run test to verify it passes**

Run: `uv run pytest tests/services/test_market_events_models.py -v`
Expected: PASS, 4 tests.

- [ ] **Step 1.5: Commit**

```bash
git add app/models/market_events.py tests/services/test_market_events_models.py
git commit -m "feat(market_events): add MarketEvent/Value/Partition ORM models (ROB-128)"
```

---

## Task 2: Create Alembic migration for market_events tables

**Files:**
- Create: `alembic/versions/a7e9c128_add_market_events_tables.py`

- [ ] **Step 2.1: Generate the migration file shell**

Manually create `alembic/versions/a7e9c128_add_market_events_tables.py` (do NOT use `--autogenerate`; we need explicit partial indexes that autogenerate gets wrong):

```python
"""add market_events tables (ROB-128)

Revision ID: a7e9c128
Revises: 2026_05_06_rob119
Create Date: 2026-05-07 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "a7e9c128"
down_revision: str | Sequence[str] | None = "2026_05_06_rob119"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "market_events",
        sa.Column("id", sa.BigInteger(), primary_key=True, nullable=False),
        sa.Column(
            "event_uuid",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            unique=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("category", sa.Text(), nullable=False),
        sa.Column("market", sa.Text(), nullable=False),
        sa.Column("country", sa.Text(), nullable=True),
        sa.Column("symbol", sa.Text(), nullable=True),
        sa.Column("company_name", sa.Text(), nullable=True),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("event_date", sa.Date(), nullable=False),
        sa.Column("release_time_utc", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("release_time_local", sa.TIMESTAMP(timezone=False), nullable=True),
        sa.Column("source_timezone", sa.Text(), nullable=True),
        sa.Column("time_hint", sa.Text(), nullable=True),
        sa.Column("importance", sa.Integer(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="scheduled"),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("source_event_id", sa.Text(), nullable=True),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("fiscal_year", sa.Integer(), nullable=True),
        sa.Column("fiscal_quarter", sa.Integer(), nullable=True),
        sa.Column(
            "raw_payload_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("fetched_at", sa.TIMESTAMP(timezone=True), nullable=True),
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
    )

    op.create_index(
        "uq_market_events_source_event_id",
        "market_events",
        ["source", "category", "market", "source_event_id"],
        unique=True,
        postgresql_where=sa.text("source_event_id IS NOT NULL"),
    )
    op.create_index(
        "uq_market_events_natural_key",
        "market_events",
        ["source", "category", "market", "symbol", "event_date", "fiscal_year", "fiscal_quarter"],
        unique=True,
        postgresql_where=sa.text("source_event_id IS NULL"),
    )
    op.create_index("ix_market_events_event_date", "market_events", ["event_date"])
    op.create_index("ix_market_events_symbol", "market_events", ["symbol"])
    op.create_index(
        "ix_market_events_category_market_date",
        "market_events",
        ["category", "market", "event_date"],
    )

    op.create_table(
        "market_event_values",
        sa.Column("id", sa.BigInteger(), primary_key=True, nullable=False),
        sa.Column(
            "event_id",
            sa.BigInteger(),
            sa.ForeignKey("market_events.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("metric_name", sa.Text(), nullable=False),
        sa.Column("period", sa.Text(), nullable=True),
        sa.Column("actual", sa.Numeric(28, 8), nullable=True),
        sa.Column("forecast", sa.Numeric(28, 8), nullable=True),
        sa.Column("previous", sa.Numeric(28, 8), nullable=True),
        sa.Column("revised_previous", sa.Numeric(28, 8), nullable=True),
        sa.Column("unit", sa.Text(), nullable=True),
        sa.Column("surprise", sa.Numeric(28, 8), nullable=True),
        sa.Column("surprise_pct", sa.Numeric(12, 4), nullable=True),
        sa.Column("released_at", sa.TIMESTAMP(timezone=True), nullable=True),
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
        sa.UniqueConstraint(
            "event_id",
            "metric_name",
            "period",
            name="uq_market_event_values_event_metric_period",
        ),
    )
    op.create_index(
        "ix_market_event_values_event_id", "market_event_values", ["event_id"]
    )

    op.create_table(
        "market_event_ingestion_partitions",
        sa.Column("id", sa.BigInteger(), primary_key=True, nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("category", sa.Text(), nullable=False),
        sa.Column("market", sa.Text(), nullable=False),
        sa.Column("partition_date", sa.Date(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("event_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("finished_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("source_request_hash", sa.Text(), nullable=True),
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
        sa.UniqueConstraint(
            "source",
            "category",
            "market",
            "partition_date",
            name="uq_market_event_ingestion_partitions_source",
        ),
    )
    op.create_index(
        "ix_market_event_ingestion_partitions_status_date",
        "market_event_ingestion_partitions",
        ["status", "partition_date"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_market_event_ingestion_partitions_status_date",
        table_name="market_event_ingestion_partitions",
    )
    op.drop_table("market_event_ingestion_partitions")

    op.drop_index("ix_market_event_values_event_id", table_name="market_event_values")
    op.drop_table("market_event_values")

    op.drop_index("ix_market_events_category_market_date", table_name="market_events")
    op.drop_index("ix_market_events_symbol", table_name="market_events")
    op.drop_index("ix_market_events_event_date", table_name="market_events")
    op.drop_index("uq_market_events_natural_key", table_name="market_events")
    op.drop_index("uq_market_events_source_event_id", table_name="market_events")
    op.drop_table("market_events")
```

- [ ] **Step 2.2: Verify alembic head detection**

Run: `uv run alembic heads`
Expected output: `a7e9c128 (head)`

- [ ] **Step 2.3: Apply migration on local Postgres (sandbox check)**

Run: `uv run alembic upgrade head`
Expected: 3 tables created, no errors.

- [ ] **Step 2.4: Verify downgrade works**

Run: `uv run alembic downgrade -1 && uv run alembic upgrade head`
Expected: clean roundtrip with no errors.

- [ ] **Step 2.5: Commit**

```bash
git add alembic/versions/a7e9c128_add_market_events_tables.py
git commit -m "feat(market_events): add alembic migration for market_events tables (ROB-128)"
```

---

## Task 3: Add taxonomy constants module

**Files:**
- Create: `app/services/market_events/__init__.py`
- Create: `app/services/market_events/taxonomy.py`
- Test: `tests/services/test_market_events_taxonomy.py`

The Linear issue lists categories, sources, statuses, and crypto sub-categories. We expose them as `frozenset` constants so the rest of the code (router validation, ingestion, tests) can reference one source of truth without coupling to an `Enum` migration.

- [ ] **Step 3.1: Write failing taxonomy test**

Create `tests/services/test_market_events_taxonomy.py`:

```python
"""Taxonomy constants for market events (ROB-128)."""

from __future__ import annotations

import pytest


@pytest.mark.unit
def test_categories_cover_required_set():
    from app.services.market_events.taxonomy import CATEGORIES

    required = {
        "earnings",
        "economic",
        "disclosure",
        "crypto_exchange_notice",
        "crypto_protocol",
        "tokenomics",
        "regulatory",
    }
    assert required <= CATEGORIES


@pytest.mark.unit
def test_markets_cover_required_set():
    from app.services.market_events.taxonomy import MARKETS

    assert {"us", "kr", "crypto", "global"} <= MARKETS


@pytest.mark.unit
def test_statuses_cover_required_set():
    from app.services.market_events.taxonomy import STATUSES

    assert {"scheduled", "released", "revised", "cancelled", "tentative"} <= STATUSES


@pytest.mark.unit
def test_time_hints_cover_required_set():
    from app.services.market_events.taxonomy import TIME_HINTS

    assert {"before_open", "after_close", "during_market", "unknown"} <= TIME_HINTS


@pytest.mark.unit
def test_partition_statuses_cover_required_set():
    from app.services.market_events.taxonomy import PARTITION_STATUSES

    assert {"pending", "running", "succeeded", "failed", "partial"} <= PARTITION_STATUSES


@pytest.mark.unit
def test_validate_category_rejects_unknown():
    from app.services.market_events.taxonomy import validate_category

    validate_category("earnings")
    with pytest.raises(ValueError, match="unknown category"):
        validate_category("not_a_category")
```

- [ ] **Step 3.2: Run test to confirm it fails**

Run: `uv run pytest tests/services/test_market_events_taxonomy.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3.3: Implement the taxonomy**

Create `app/services/market_events/__init__.py` with empty body.

Create `app/services/market_events/taxonomy.py`:

```python
"""Market event taxonomy: categories, markets, statuses, time hints (ROB-128).

Single source of truth for enum-like sets used by ingestion, query, and router code.
"""

from __future__ import annotations

CATEGORIES: frozenset[str] = frozenset(
    {
        "earnings",
        "economic",
        "disclosure",
        "crypto_exchange_notice",
        "crypto_protocol",
        "tokenomics",
        "regulatory",
    }
)

MARKETS: frozenset[str] = frozenset({"us", "kr", "crypto", "global"})

STATUSES: frozenset[str] = frozenset(
    {"scheduled", "released", "revised", "cancelled", "tentative"}
)

TIME_HINTS: frozenset[str] = frozenset(
    {"before_open", "after_close", "during_market", "unknown"}
)

PARTITION_STATUSES: frozenset[str] = frozenset(
    {"pending", "running", "succeeded", "failed", "partial"}
)

SOURCES: frozenset[str] = frozenset(
    {"finnhub", "dart", "upbit", "bithumb", "binance", "token_unlocks"}
)


def validate_category(category: str) -> None:
    if category not in CATEGORIES:
        raise ValueError(f"unknown category: {category!r}")


def validate_market(market: str) -> None:
    if market not in MARKETS:
        raise ValueError(f"unknown market: {market!r}")


def validate_partition_status(status: str) -> None:
    if status not in PARTITION_STATUSES:
        raise ValueError(f"unknown partition status: {status!r}")
```

- [ ] **Step 3.4: Run test to confirm pass**

Run: `uv run pytest tests/services/test_market_events_taxonomy.py -v`
Expected: 6 PASS.

- [ ] **Step 3.5: Commit**

```bash
git add app/services/market_events/__init__.py app/services/market_events/taxonomy.py tests/services/test_market_events_taxonomy.py
git commit -m "feat(market_events): add taxonomy constants for categories/markets/statuses (ROB-128)"
```

---

## Task 4: Add Pydantic response schemas

**Files:**
- Create: `app/schemas/market_events.py`
- Test: `tests/services/test_market_events_schemas.py`

Response shape from the spec:

```json
{
  "date": "2026-05-07",
  "events": [
    {"category": "earnings", "market": "us", "symbol": "IONQ", ...,
     "held": true, "watched": false,
     "values": [{"metric_name": "eps", "actual": -0.38, "forecast": -0.3593, "unit": "USD"}]}
  ]
}
```

- [ ] **Step 4.1: Write failing schema validation test**

Create `tests/services/test_market_events_schemas.py`:

```python
"""Pydantic schema tests for market events (ROB-128)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest


@pytest.mark.unit
def test_market_event_value_response_round_trip():
    from app.schemas.market_events import MarketEventValueResponse

    payload = {
        "metric_name": "eps",
        "period": "Q1-2026",
        "actual": Decimal("-0.38"),
        "forecast": Decimal("-0.3593"),
        "previous": None,
        "unit": "USD",
        "surprise": None,
        "surprise_pct": None,
    }
    obj = MarketEventValueResponse.model_validate(payload)
    assert obj.metric_name == "eps"
    assert float(obj.actual) == pytest.approx(-0.38)


@pytest.mark.unit
def test_market_event_response_includes_held_watched():
    from app.schemas.market_events import MarketEventResponse, MarketEventValueResponse

    obj = MarketEventResponse(
        category="earnings",
        market="us",
        symbol="IONQ",
        title="IONQ earnings release",
        event_date=date(2026, 5, 7),
        time_hint="after_close",
        held=True,
        watched=False,
        values=[],
        source="finnhub",
        source_event_id=None,
    )
    dumped = obj.model_dump()
    assert dumped["held"] is True
    assert dumped["watched"] is False
    assert dumped["values"] == []


@pytest.mark.unit
def test_market_events_day_response_shape():
    from app.schemas.market_events import MarketEventsDayResponse

    obj = MarketEventsDayResponse(date=date(2026, 5, 7), events=[])
    assert obj.model_dump()["date"] == date(2026, 5, 7)
```

- [ ] **Step 4.2: Run test to confirm it fails**

Run: `uv run pytest tests/services/test_market_events_schemas.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 4.3: Implement the schemas**

Create `app/schemas/market_events.py`:

```python
"""Pydantic response schemas for market events (ROB-128)."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class MarketEventValueResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    metric_name: str
    period: str | None = None
    actual: Decimal | None = None
    forecast: Decimal | None = None
    previous: Decimal | None = None
    revised_previous: Decimal | None = None
    unit: str | None = None
    surprise: Decimal | None = None
    surprise_pct: Decimal | None = None
    released_at: datetime | None = None


class MarketEventResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    category: str
    market: str
    country: str | None = None
    symbol: str | None = None
    company_name: str | None = None
    title: str | None = None
    event_date: date
    release_time_utc: datetime | None = None
    time_hint: str | None = None
    importance: int | None = None
    status: str = "scheduled"
    source: str
    source_event_id: str | None = None
    source_url: str | None = None
    fiscal_year: int | None = None
    fiscal_quarter: int | None = None

    held: bool | None = None
    watched: bool | None = None

    values: list[MarketEventValueResponse] = Field(default_factory=list)


class MarketEventsDayResponse(BaseModel):
    date: date
    events: list[MarketEventResponse] = Field(default_factory=list)


class MarketEventsRangeResponse(BaseModel):
    from_date: date
    to_date: date
    count: int
    events: list[MarketEventResponse] = Field(default_factory=list)


class IngestionPartitionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    source: str
    category: str
    market: str
    partition_date: date
    status: str
    event_count: int
    started_at: datetime | None = None
    finished_at: datetime | None = None
    last_error: str | None = None
    retry_count: int


class IngestionRunResult(BaseModel):
    source: str
    category: str
    market: str
    partition_date: date
    status: str
    event_count: int
    error: str | None = None
```

- [ ] **Step 4.4: Run test**

Run: `uv run pytest tests/services/test_market_events_schemas.py -v`
Expected: 3 PASS.

- [ ] **Step 4.5: Commit**

```bash
git add app/schemas/market_events.py tests/services/test_market_events_schemas.py
git commit -m "feat(market_events): add pydantic response schemas (ROB-128)"
```

---

## Task 5: Finnhub earnings normalizer (pure function)

**Files:**
- Create: `app/services/market_events/normalizers.py`
- Test: `tests/services/test_market_events_normalizers.py`

The Finnhub helper already returns rows shaped like (see `app/mcp_server/tooling/fundamentals_sources_finnhub.py:208-221`):

```json
{"symbol": "IONQ", "date": "2026-05-07", "hour": "amc",
 "eps_estimate": -0.3593, "eps_actual": -0.38,
 "revenue_estimate": 50729332, "revenue_actual": 64670000,
 "quarter": 1, "year": 2026}
```

- [ ] **Step 5.1: Write failing normalizer test**

Create `tests/services/test_market_events_normalizers.py`:

```python
"""Pure-function normalizer tests (ROB-128)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest


FINNHUB_ROW_AMC = {
    "symbol": "IONQ",
    "date": "2026-05-07",
    "hour": "amc",
    "eps_estimate": -0.3593,
    "eps_actual": -0.38,
    "revenue_estimate": 50729332,
    "revenue_actual": 64670000,
    "quarter": 1,
    "year": 2026,
}

FINNHUB_ROW_BMO = {
    "symbol": "NVDA",
    "date": "2026-05-08",
    "hour": "bmo",
    "eps_estimate": 0.5,
    "eps_actual": None,
    "revenue_estimate": 1_000_000_000,
    "revenue_actual": None,
    "quarter": 1,
    "year": 2026,
}


@pytest.mark.unit
def test_normalize_finnhub_earnings_amc_returns_after_close_hint():
    from app.services.market_events.normalizers import normalize_finnhub_earnings_row

    event_dict, value_dicts = normalize_finnhub_earnings_row(FINNHUB_ROW_AMC)

    assert event_dict["category"] == "earnings"
    assert event_dict["market"] == "us"
    assert event_dict["symbol"] == "IONQ"
    assert event_dict["event_date"] == date(2026, 5, 7)
    assert event_dict["time_hint"] == "after_close"
    assert event_dict["source"] == "finnhub"
    assert event_dict["fiscal_year"] == 2026
    assert event_dict["fiscal_quarter"] == 1
    assert event_dict["status"] == "released"
    assert event_dict["source_event_id"] is None  # Finnhub does not provide id

    metrics = {v["metric_name"]: v for v in value_dicts}
    assert "eps" in metrics
    assert metrics["eps"]["actual"] == Decimal("-0.38")
    assert metrics["eps"]["forecast"] == Decimal("-0.3593")
    assert metrics["eps"]["unit"] == "USD"
    assert metrics["revenue"]["actual"] == Decimal("64670000")
    assert metrics["revenue"]["forecast"] == Decimal("50729332")


@pytest.mark.unit
def test_normalize_finnhub_earnings_bmo_returns_before_open_hint():
    from app.services.market_events.normalizers import normalize_finnhub_earnings_row

    event_dict, _ = normalize_finnhub_earnings_row(FINNHUB_ROW_BMO)

    assert event_dict["time_hint"] == "before_open"
    assert event_dict["status"] == "scheduled"  # actual is None


@pytest.mark.unit
def test_normalize_finnhub_earnings_unknown_hour_falls_back_to_unknown():
    from app.services.market_events.normalizers import normalize_finnhub_earnings_row

    row = {**FINNHUB_ROW_AMC, "hour": ""}
    event_dict, _ = normalize_finnhub_earnings_row(row)
    assert event_dict["time_hint"] == "unknown"


@pytest.mark.unit
def test_normalize_finnhub_earnings_skips_value_when_both_actual_and_forecast_missing():
    from app.services.market_events.normalizers import normalize_finnhub_earnings_row

    row = {
        **FINNHUB_ROW_AMC,
        "revenue_estimate": None,
        "revenue_actual": None,
    }
    _, value_dicts = normalize_finnhub_earnings_row(row)
    metrics = {v["metric_name"] for v in value_dicts}
    assert "revenue" not in metrics
    assert "eps" in metrics
```

- [ ] **Step 5.2: Run test to confirm it fails**

Run: `uv run pytest tests/services/test_market_events_normalizers.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 5.3: Implement the Finnhub normalizer**

Create `app/services/market_events/normalizers.py`:

```python
"""Pure-function normalizers from external source rows to MarketEvent dicts (ROB-128).

These functions never touch the database. They produce dicts shaped to be passed
to MarketEventsRepository.upsert_event_with_values.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any


_FINNHUB_HOUR_TO_TIME_HINT = {
    "bmo": "before_open",
    "amc": "after_close",
    "dmh": "during_market",
    "dmt": "during_market",
}


def _to_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _classify_finnhub_status(eps_actual: Any, revenue_actual: Any) -> str:
    if eps_actual is not None or revenue_actual is not None:
        return "released"
    return "scheduled"


def normalize_finnhub_earnings_row(
    row: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Normalize one Finnhub `earningsCalendar` item.

    Returns (event_dict, [value_dict, ...]) ready for upsert.
    """
    symbol = (row.get("symbol") or "").strip().upper()
    raw_date = row.get("date")
    if not symbol or not raw_date:
        raise ValueError("finnhub row missing symbol or date")

    event_date = date.fromisoformat(raw_date)
    hour = (row.get("hour") or "").strip().lower()
    time_hint = _FINNHUB_HOUR_TO_TIME_HINT.get(hour, "unknown")
    eps_actual = row.get("eps_actual")
    revenue_actual = row.get("revenue_actual")

    event = {
        "category": "earnings",
        "market": "us",
        "country": "US",
        "symbol": symbol,
        "company_name": None,
        "title": f"{symbol} earnings release",
        "event_date": event_date,
        "release_time_utc": None,
        "release_time_local": None,
        "source_timezone": "America/New_York",
        "time_hint": time_hint,
        "importance": None,
        "status": _classify_finnhub_status(eps_actual, revenue_actual),
        "source": "finnhub",
        "source_event_id": None,
        "source_url": None,
        "fiscal_year": row.get("year"),
        "fiscal_quarter": row.get("quarter"),
        "raw_payload_json": dict(row),
    }

    period = None
    if event["fiscal_year"] is not None and event["fiscal_quarter"] is not None:
        period = f"Q{event['fiscal_quarter']}-{event['fiscal_year']}"

    values: list[dict[str, Any]] = []
    eps_forecast = row.get("eps_estimate")
    if eps_actual is not None or eps_forecast is not None:
        values.append(
            {
                "metric_name": "eps",
                "period": period,
                "actual": _to_decimal(eps_actual),
                "forecast": _to_decimal(eps_forecast),
                "unit": "USD",
            }
        )
    rev_forecast = row.get("revenue_estimate")
    if revenue_actual is not None or rev_forecast is not None:
        values.append(
            {
                "metric_name": "revenue",
                "period": period,
                "actual": _to_decimal(revenue_actual),
                "forecast": _to_decimal(rev_forecast),
                "unit": "USD",
            }
        )

    return event, values


_DART_EARNINGS_KEYWORDS = (
    "분기보고서",
    "반기보고서",
    "사업보고서",
    "영업실적",
    "잠정실적",
    "매출액또는손익구조",
    "영업손실",
    "영업이익",
    "실적",
    "전망",
)


def classify_dart_category(report_nm: str) -> str:
    """Map a DART report_nm string to our category taxonomy."""
    if not report_nm:
        return "disclosure"
    for kw in _DART_EARNINGS_KEYWORDS:
        if kw in report_nm:
            return "earnings"
    return "disclosure"


def normalize_dart_disclosure_row(
    row: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Normalize one DART `list_date` row to a MarketEvent.

    DART rows expose at minimum: rcept_no, rcept_dt, corp_name, corp_code, report_nm.
    URL builds from rcept_no.
    """
    rcept_no = (row.get("rcept_no") or row.get("rcp_no") or "").strip()
    rcept_dt = (row.get("rcept_dt") or row.get("date") or "").strip()
    corp_name = (row.get("corp_name") or "").strip()
    report_nm = (row.get("report_nm") or "").strip()
    corp_code = (row.get("corp_code") or "").strip() or None

    if not rcept_no or not rcept_dt:
        raise ValueError("dart row missing rcept_no or rcept_dt")

    if len(rcept_dt) >= 8 and rcept_dt[:8].isdigit():
        event_date = date(int(rcept_dt[:4]), int(rcept_dt[4:6]), int(rcept_dt[6:8]))
    else:
        event_date = date.fromisoformat(rcept_dt)

    category = classify_dart_category(report_nm)

    event = {
        "category": category,
        "market": "kr",
        "country": "KR",
        "symbol": corp_code,
        "company_name": corp_name,
        "title": report_nm or None,
        "event_date": event_date,
        "release_time_utc": None,
        "release_time_local": None,
        "source_timezone": "Asia/Seoul",
        "time_hint": "unknown",
        "importance": None,
        "status": "released",
        "source": "dart",
        "source_event_id": rcept_no,
        "source_url": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no}",
        "fiscal_year": None,
        "fiscal_quarter": None,
        "raw_payload_json": dict(row),
    }
    return event, []
```

- [ ] **Step 5.4: Run test**

Run: `uv run pytest tests/services/test_market_events_normalizers.py -v`
Expected: 4 PASS.

- [ ] **Step 5.5: Add DART normalizer test**

Append to `tests/services/test_market_events_normalizers.py`:

```python
DART_ROW_QUARTERLY = {
    "rcept_no": "20260507000123",
    "rcept_dt": "20260507",
    "corp_name": "삼성전자",
    "corp_code": "00126380",
    "report_nm": "분기보고서 (2026.03)",
}

DART_ROW_OTHER = {
    "rcept_no": "20260507000456",
    "rcept_dt": "20260507",
    "corp_name": "현대차",
    "corp_code": "00164742",
    "report_nm": "감사인지정",
}


@pytest.mark.unit
def test_normalize_dart_quarterly_classifies_as_earnings():
    from app.services.market_events.normalizers import normalize_dart_disclosure_row

    event, values = normalize_dart_disclosure_row(DART_ROW_QUARTERLY)
    assert event["category"] == "earnings"
    assert event["market"] == "kr"
    assert event["source"] == "dart"
    assert event["source_event_id"] == "20260507000123"
    assert event["symbol"] == "00126380"
    assert event["company_name"] == "삼성전자"
    assert "rcpNo=20260507000123" in event["source_url"]
    assert event["event_date"] == date(2026, 5, 7)
    assert values == []


@pytest.mark.unit
def test_normalize_dart_unrelated_filing_classifies_as_disclosure():
    from app.services.market_events.normalizers import normalize_dart_disclosure_row

    event, _ = normalize_dart_disclosure_row(DART_ROW_OTHER)
    assert event["category"] == "disclosure"
```

Run: `uv run pytest tests/services/test_market_events_normalizers.py -v`
Expected: 6 PASS total.

- [ ] **Step 5.6: Commit**

```bash
git add app/services/market_events/normalizers.py tests/services/test_market_events_normalizers.py
git commit -m "feat(market_events): add finnhub + dart row normalizers (ROB-128)"
```

---

## Task 6: Repository — idempotent upsert (DB)

**Files:**
- Create: `app/services/market_events/repository.py`
- Test: `tests/services/test_market_events_repository.py`

This is the **only place** in the new code allowed to write to the three tables.

- [ ] **Step 6.1: Write failing repository upsert test**

Create `tests/services/test_market_events_repository.py`:

```python
"""DB-backed tests for MarketEventsRepository (ROB-128)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select


@pytest.mark.integration
async def test_upsert_event_with_values_inserts_event_and_values(db_session):
    from app.models.market_events import MarketEvent, MarketEventValue
    from app.services.market_events.repository import MarketEventsRepository

    repo = MarketEventsRepository(db_session)
    event_dict = {
        "category": "earnings",
        "market": "us",
        "country": "US",
        "symbol": "IONQ",
        "title": "IONQ earnings release",
        "event_date": date(2026, 5, 7),
        "time_hint": "after_close",
        "status": "released",
        "source": "finnhub",
        "source_event_id": None,
        "fiscal_year": 2026,
        "fiscal_quarter": 1,
        "raw_payload_json": {"symbol": "IONQ"},
    }
    values = [
        {"metric_name": "eps", "period": "Q1-2026",
         "actual": Decimal("-0.38"), "forecast": Decimal("-0.36"), "unit": "USD"},
    ]

    event = await repo.upsert_event_with_values(event_dict, values)
    await db_session.commit()

    rows = (await db_session.execute(select(MarketEvent))).scalars().all()
    assert len(rows) == 1
    assert rows[0].symbol == "IONQ"
    vrows = (await db_session.execute(select(MarketEventValue))).scalars().all()
    assert len(vrows) == 1
    assert vrows[0].event_id == event.id


@pytest.mark.integration
async def test_upsert_event_is_idempotent_on_natural_key(db_session):
    from app.models.market_events import MarketEvent, MarketEventValue
    from app.services.market_events.repository import MarketEventsRepository

    repo = MarketEventsRepository(db_session)
    event_dict = {
        "category": "earnings",
        "market": "us",
        "symbol": "IONQ",
        "title": "IONQ earnings release",
        "event_date": date(2026, 5, 7),
        "status": "scheduled",
        "source": "finnhub",
        "source_event_id": None,
        "fiscal_year": 2026,
        "fiscal_quarter": 1,
    }
    values = [
        {"metric_name": "eps", "period": "Q1-2026", "forecast": Decimal("-0.36"), "unit": "USD"},
    ]
    await repo.upsert_event_with_values(event_dict, values)
    await db_session.commit()

    # Second call with the same natural key + updated status/value
    event_dict_v2 = {**event_dict, "status": "released"}
    values_v2 = [
        {"metric_name": "eps", "period": "Q1-2026",
         "actual": Decimal("-0.38"), "forecast": Decimal("-0.36"), "unit": "USD"},
    ]
    await repo.upsert_event_with_values(event_dict_v2, values_v2)
    await db_session.commit()

    rows = (await db_session.execute(select(MarketEvent))).scalars().all()
    assert len(rows) == 1
    assert rows[0].status == "released"
    vrows = (await db_session.execute(select(MarketEventValue))).scalars().all()
    assert len(vrows) == 1
    assert vrows[0].actual == Decimal("-0.38")


@pytest.mark.integration
async def test_upsert_event_with_source_event_id_uses_id_key(db_session):
    from app.services.market_events.repository import MarketEventsRepository
    from app.models.market_events import MarketEvent

    repo = MarketEventsRepository(db_session)
    event_dict = {
        "category": "disclosure",
        "market": "kr",
        "symbol": "00126380",
        "title": "분기보고서",
        "event_date": date(2026, 5, 7),
        "status": "released",
        "source": "dart",
        "source_event_id": "20260507000123",
    }
    await repo.upsert_event_with_values(event_dict, [])
    await db_session.commit()

    # Same source_event_id with updated title
    await repo.upsert_event_with_values({**event_dict, "title": "분기보고서 (2026.03)"}, [])
    await db_session.commit()

    rows = (await db_session.execute(select(MarketEvent))).scalars().all()
    assert len(rows) == 1
    assert rows[0].title == "분기보고서 (2026.03)"
```

- [ ] **Step 6.2: Run test to confirm it fails**

Run: `uv run pytest tests/services/test_market_events_repository.py -v`
Expected: FAIL with `ModuleNotFoundError` (db_session fixture itself works — that's already in conftest).

- [ ] **Step 6.3: Implement the repository**

Create `app/services/market_events/repository.py`:

```python
"""Market events repository — only place that writes the three market_events tables (ROB-128).

Idempotency strategy:
* When `source_event_id` is provided, upsert keyed by (source, category, market, source_event_id).
* Otherwise upsert keyed by (source, category, market, symbol, event_date, fiscal_year, fiscal_quarter).
Both keys are enforced by partial unique indexes (see migration).

Values are upserted by (event_id, metric_name, period).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.market_events import (
    MarketEvent,
    MarketEventIngestionPartition,
    MarketEventValue,
)
from app.services.alpaca_paper_ledger_service import _redact_sensitive_keys


class MarketEventsRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def upsert_event_with_values(
        self,
        event_data: dict[str, Any],
        values: list[dict[str, Any]],
    ) -> MarketEvent:
        payload = dict(event_data)
        if payload.get("raw_payload_json") is not None:
            payload["raw_payload_json"] = _redact_sensitive_keys(payload["raw_payload_json"])

        natural_keys = (
            "source",
            "category",
            "market",
            "symbol",
            "event_date",
            "fiscal_year",
            "fiscal_quarter",
        )
        update_columns = {
            k: payload.get(k)
            for k in (
                "country",
                "company_name",
                "title",
                "release_time_utc",
                "release_time_local",
                "source_timezone",
                "time_hint",
                "importance",
                "status",
                "source_url",
                "raw_payload_json",
                "fetched_at",
            )
            if k in payload
        }

        if payload.get("source_event_id"):
            stmt = (
                pg_insert(MarketEvent.__table__)
                .values(**payload)
                .on_conflict_do_update(
                    index_elements=["source", "category", "market", "source_event_id"],
                    index_where=MarketEvent.__table__.c.source_event_id.isnot(None),
                    set_=update_columns,
                )
                .returning(MarketEvent.__table__.c.id)
            )
        else:
            stmt = (
                pg_insert(MarketEvent.__table__)
                .values(**payload)
                .on_conflict_do_update(
                    index_elements=list(natural_keys),
                    index_where=MarketEvent.__table__.c.source_event_id.is_(None),
                    set_=update_columns,
                )
                .returning(MarketEvent.__table__.c.id)
            )

        result = await self.db.execute(stmt)
        event_id = result.scalar_one()

        for value in values:
            await self._upsert_value(event_id, value)

        await self.db.flush()
        event = (
            await self.db.execute(
                select(MarketEvent).where(MarketEvent.id == event_id)
            )
        ).scalar_one()
        return event

    async def _upsert_value(self, event_id: int, value: dict[str, Any]) -> None:
        payload = {**value, "event_id": event_id}
        update_columns = {
            k: payload.get(k)
            for k in (
                "actual",
                "forecast",
                "previous",
                "revised_previous",
                "unit",
                "surprise",
                "surprise_pct",
                "released_at",
            )
            if k in payload
        }
        stmt = (
            pg_insert(MarketEventValue.__table__)
            .values(**payload)
            .on_conflict_do_update(
                constraint="uq_market_event_values_event_metric_period",
                set_=update_columns,
            )
        )
        await self.db.execute(stmt)

    # -- partition state ----------------------------------------------------

    async def get_or_create_partition(
        self,
        *,
        source: str,
        category: str,
        market: str,
        partition_date: date,
    ) -> MarketEventIngestionPartition:
        existing = (
            await self.db.execute(
                select(MarketEventIngestionPartition).where(
                    MarketEventIngestionPartition.source == source,
                    MarketEventIngestionPartition.category == category,
                    MarketEventIngestionPartition.market == market,
                    MarketEventIngestionPartition.partition_date == partition_date,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing

        row = MarketEventIngestionPartition(
            source=source,
            category=category,
            market=market,
            partition_date=partition_date,
            status="pending",
        )
        self.db.add(row)
        await self.db.flush()
        return row

    async def mark_partition_running(
        self, partition: MarketEventIngestionPartition
    ) -> None:
        partition.status = "running"
        partition.started_at = datetime.utcnow()
        partition.last_error = None
        await self.db.flush()

    async def mark_partition_succeeded(
        self,
        partition: MarketEventIngestionPartition,
        *,
        event_count: int,
    ) -> None:
        partition.status = "succeeded"
        partition.event_count = event_count
        partition.finished_at = datetime.utcnow()
        partition.last_error = None
        await self.db.flush()

    async def mark_partition_failed(
        self,
        partition: MarketEventIngestionPartition,
        *,
        error: str,
    ) -> None:
        partition.status = "failed"
        partition.finished_at = datetime.utcnow()
        partition.last_error = error[:2000]
        partition.retry_count = (partition.retry_count or 0) + 1
        await self.db.flush()
```

- [ ] **Step 6.4: Run test**

Run: `uv run pytest tests/services/test_market_events_repository.py -v`
Expected: 3 PASS (requires Postgres at `DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/test_db`).

- [ ] **Step 6.5: Add partition lifecycle test**

Append to `tests/services/test_market_events_repository.py`:

```python
@pytest.mark.integration
async def test_partition_lifecycle_records_running_succeeded(db_session):
    from app.services.market_events.repository import MarketEventsRepository

    repo = MarketEventsRepository(db_session)
    p = await repo.get_or_create_partition(
        source="finnhub", category="earnings", market="us",
        partition_date=date(2026, 5, 7),
    )
    assert p.status == "pending"

    await repo.mark_partition_running(p)
    assert p.status == "running"
    assert p.started_at is not None

    await repo.mark_partition_succeeded(p, event_count=42)
    assert p.status == "succeeded"
    assert p.event_count == 42
    assert p.finished_at is not None
    await db_session.commit()


@pytest.mark.integration
async def test_partition_failure_increments_retry_count(db_session):
    from app.services.market_events.repository import MarketEventsRepository

    repo = MarketEventsRepository(db_session)
    p = await repo.get_or_create_partition(
        source="finnhub", category="earnings", market="us",
        partition_date=date(2026, 5, 8),
    )
    await repo.mark_partition_failed(p, error="read timeout")
    assert p.status == "failed"
    assert p.retry_count == 1
    assert p.last_error == "read timeout"

    await repo.mark_partition_failed(p, error="another")
    assert p.retry_count == 2
    await db_session.commit()
```

Run: `uv run pytest tests/services/test_market_events_repository.py -v`
Expected: 5 PASS.

- [ ] **Step 6.6: Commit**

```bash
git add app/services/market_events/repository.py tests/services/test_market_events_repository.py
git commit -m "feat(market_events): add repository with idempotent upsert + partition state (ROB-128)"
```

---

## Task 7: Ingestion orchestrator for US Finnhub earnings (one-day partitions)

**Files:**
- Create: `app/services/market_events/ingestion.py`
- Test: `tests/services/test_market_events_ingestion.py`

The orchestrator wraps: claim partition → call Finnhub for one day → normalize → upsert → mark succeeded (or failed). It calls the existing `_fetch_earnings_calendar_finnhub` so we don't re-implement the client.

- [ ] **Step 7.1: Write failing ingestion test (mocked Finnhub)**

Create `tests/services/test_market_events_ingestion.py`:

```python
"""Ingestion orchestration tests (ROB-128)."""

from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select


FINNHUB_RESPONSE_ONE_ROW = {
    "symbol": None,
    "instrument_type": "equity_us",
    "source": "finnhub",
    "from_date": "2026-05-07",
    "to_date": "2026-05-07",
    "count": 1,
    "earnings": [
        {
            "symbol": "IONQ",
            "date": "2026-05-07",
            "hour": "amc",
            "eps_estimate": -0.3593,
            "eps_actual": -0.38,
            "revenue_estimate": 50729332,
            "revenue_actual": 64670000,
            "quarter": 1,
            "year": 2026,
        }
    ],
}


@pytest.mark.integration
async def test_ingest_us_earnings_for_date_succeeds(db_session, monkeypatch):
    from app.models.market_events import MarketEvent, MarketEventIngestionPartition
    from app.services.market_events import ingestion

    fake = AsyncMock(return_value=FINNHUB_RESPONSE_ONE_ROW)
    monkeypatch.setattr(ingestion, "_fetch_earnings_calendar_finnhub", fake)

    result = await ingestion.ingest_us_earnings_for_date(
        db_session, date(2026, 5, 7)
    )
    await db_session.commit()

    assert result.status == "succeeded"
    assert result.event_count == 1
    fake.assert_awaited_once_with(None, "2026-05-07", "2026-05-07")

    events = (await db_session.execute(select(MarketEvent))).scalars().all()
    assert len(events) == 1
    assert events[0].symbol == "IONQ"

    parts = (
        await db_session.execute(select(MarketEventIngestionPartition))
    ).scalars().all()
    assert len(parts) == 1
    assert parts[0].status == "succeeded"
    assert parts[0].event_count == 1


@pytest.mark.integration
async def test_ingest_us_earnings_for_date_records_failure(db_session, monkeypatch):
    from app.models.market_events import MarketEventIngestionPartition
    from app.services.market_events import ingestion

    fake = AsyncMock(side_effect=TimeoutError("read timeout=10"))
    monkeypatch.setattr(ingestion, "_fetch_earnings_calendar_finnhub", fake)

    result = await ingestion.ingest_us_earnings_for_date(
        db_session, date(2026, 5, 8)
    )
    await db_session.commit()

    assert result.status == "failed"
    assert result.event_count == 0
    assert "read timeout" in (result.error or "")

    parts = (
        await db_session.execute(select(MarketEventIngestionPartition))
    ).scalars().all()
    assert len(parts) == 1
    assert parts[0].status == "failed"
    assert parts[0].retry_count == 1


@pytest.mark.integration
async def test_ingest_us_earnings_for_date_is_idempotent(db_session, monkeypatch):
    from app.models.market_events import MarketEvent, MarketEventIngestionPartition
    from app.services.market_events import ingestion

    fake = AsyncMock(return_value=FINNHUB_RESPONSE_ONE_ROW)
    monkeypatch.setattr(ingestion, "_fetch_earnings_calendar_finnhub", fake)

    await ingestion.ingest_us_earnings_for_date(db_session, date(2026, 5, 7))
    await db_session.commit()
    await ingestion.ingest_us_earnings_for_date(db_session, date(2026, 5, 7))
    await db_session.commit()

    events = (await db_session.execute(select(MarketEvent))).scalars().all()
    assert len(events) == 1
    parts = (
        await db_session.execute(select(MarketEventIngestionPartition))
    ).scalars().all()
    assert len(parts) == 1
    assert parts[0].status == "succeeded"
```

- [ ] **Step 7.2: Run test to confirm it fails**

Run: `uv run pytest tests/services/test_market_events_ingestion.py -v`
Expected: FAIL with `ModuleNotFoundError: app.services.market_events.ingestion`.

- [ ] **Step 7.3: Implement the ingestion module**

Create `app/services/market_events/ingestion.py`:

```python
"""Per-day ingestion orchestrators (ROB-128).

Each `ingest_*_for_date` function:
  1. claims a row in market_event_ingestion_partitions (running),
  2. fetches one day of source data,
  3. normalizes + upserts into market_events / market_event_values,
  4. marks the partition succeeded (with event_count) or failed (with last_error).

These functions are pure ingestion: no broker / order / watch / scheduling side effects.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.mcp_server.tooling.fundamentals_sources_finnhub import (
    _fetch_earnings_calendar_finnhub,
)
from app.schemas.market_events import IngestionRunResult
from app.services.market_events.normalizers import (
    normalize_dart_disclosure_row,
    normalize_finnhub_earnings_row,
)
from app.services.market_events.repository import MarketEventsRepository

logger = logging.getLogger(__name__)


async def ingest_us_earnings_for_date(
    db: AsyncSession,
    target_date: date,
) -> IngestionRunResult:
    repo = MarketEventsRepository(db)
    partition = await repo.get_or_create_partition(
        source="finnhub",
        category="earnings",
        market="us",
        partition_date=target_date,
    )
    await repo.mark_partition_running(partition)

    iso = target_date.isoformat()
    try:
        response = await _fetch_earnings_calendar_finnhub(None, iso, iso)
    except Exception as exc:
        await repo.mark_partition_failed(partition, error=str(exc))
        logger.exception("finnhub earnings fetch failed for %s", iso)
        return IngestionRunResult(
            source="finnhub",
            category="earnings",
            market="us",
            partition_date=target_date,
            status="failed",
            event_count=0,
            error=str(exc),
        )

    rows = response.get("earnings", []) if isinstance(response, dict) else []
    upserted = 0
    for row in rows:
        try:
            event_dict, value_dicts = normalize_finnhub_earnings_row(row)
        except ValueError as exc:
            logger.warning("skipping unparseable finnhub row: %s (%s)", row, exc)
            continue
        await repo.upsert_event_with_values(event_dict, value_dicts)
        upserted += 1

    await repo.mark_partition_succeeded(partition, event_count=upserted)
    return IngestionRunResult(
        source="finnhub",
        category="earnings",
        market="us",
        partition_date=target_date,
        status="succeeded",
        event_count=upserted,
    )


async def ingest_kr_disclosures_for_date(
    db: AsyncSession,
    target_date: date,
    fetch_rows: Any | None = None,
) -> IngestionRunResult:
    """Ingest KR DART disclosures for one day.

    `fetch_rows` is an optional injection point: an async callable taking a date and
    returning a list of dart-row dicts. Default uses
    `app.services.market_events.dart_helpers.fetch_dart_filings_for_date`.
    """
    if fetch_rows is None:
        from app.services.market_events.dart_helpers import (
            fetch_dart_filings_for_date as _default_fetch,
        )
        fetch_rows = _default_fetch

    repo = MarketEventsRepository(db)
    partition = await repo.get_or_create_partition(
        source="dart",
        category="disclosure",
        market="kr",
        partition_date=target_date,
    )
    await repo.mark_partition_running(partition)

    try:
        rows = await fetch_rows(target_date)
    except Exception as exc:
        await repo.mark_partition_failed(partition, error=str(exc))
        logger.exception("dart fetch failed for %s", target_date)
        return IngestionRunResult(
            source="dart",
            category="disclosure",
            market="kr",
            partition_date=target_date,
            status="failed",
            event_count=0,
            error=str(exc),
        )

    upserted = 0
    for row in rows:
        try:
            event_dict, value_dicts = normalize_dart_disclosure_row(row)
        except ValueError as exc:
            logger.warning("skipping unparseable dart row: %s (%s)", row, exc)
            continue
        await repo.upsert_event_with_values(event_dict, value_dicts)
        upserted += 1

    await repo.mark_partition_succeeded(partition, event_count=upserted)
    return IngestionRunResult(
        source="dart",
        category="disclosure",
        market="kr",
        partition_date=target_date,
        status="succeeded",
        event_count=upserted,
    )
```

- [ ] **Step 7.4: Add the DART helper (optional fetcher)**

Create `app/services/market_events/dart_helpers.py`:

```python
"""Thin wrapper around OpenDartReader.list_date for per-day market-wide DART fetch (ROB-128).

This is the only DART-side new code; for per-symbol filings the existing
`app/services/disclosures/dart.py::list_filings` is reused elsewhere.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date
from typing import Any

from app.services.disclosures.dart import _get_client

logger = logging.getLogger(__name__)


async def fetch_dart_filings_for_date(target_date: date) -> list[dict[str, Any]]:
    """Return DART filings for one day. Empty list if DART is unavailable.

    The OpenDartReader client is loaded lazily and reused across calls.
    """
    client = await _get_client()
    if client is None:
        logger.warning("DART client unavailable; skipping fetch for %s", target_date)
        return []

    iso = target_date.isoformat()

    def fetch_sync() -> list[dict[str, Any]]:
        df = client.list_date(iso)
        if df is None or df.empty:
            return []
        return df.to_dict(orient="records")

    return await asyncio.to_thread(fetch_sync)
```

- [ ] **Step 7.5: Run tests**

Run: `uv run pytest tests/services/test_market_events_ingestion.py -v`
Expected: 3 PASS.

- [ ] **Step 7.6: Add KR disclosure ingestion test**

Append to `tests/services/test_market_events_ingestion.py`:

```python
DART_ROW = {
    "rcept_no": "20260507000123",
    "rcept_dt": "20260507",
    "corp_name": "삼성전자",
    "corp_code": "00126380",
    "report_nm": "분기보고서 (2026.03)",
}


@pytest.mark.integration
async def test_ingest_kr_disclosures_for_date_with_injected_fetcher(db_session):
    from app.models.market_events import MarketEvent
    from app.services.market_events import ingestion

    async def fake_fetch(d):
        assert d == date(2026, 5, 7)
        return [DART_ROW]

    result = await ingestion.ingest_kr_disclosures_for_date(
        db_session, date(2026, 5, 7), fetch_rows=fake_fetch
    )
    await db_session.commit()
    assert result.status == "succeeded"
    assert result.event_count == 1

    rows = (await db_session.execute(select(MarketEvent))).scalars().all()
    assert len(rows) == 1
    assert rows[0].source == "dart"
    assert rows[0].source_event_id == "20260507000123"
```

Run: `uv run pytest tests/services/test_market_events_ingestion.py -v`
Expected: 4 PASS.

- [ ] **Step 7.7: Commit**

```bash
git add app/services/market_events/ingestion.py app/services/market_events/dart_helpers.py tests/services/test_market_events_ingestion.py
git commit -m "feat(market_events): add per-day finnhub + dart ingestion orchestrators (ROB-128)"
```

---

## Task 8: Read-only query service (date range + held/watched placeholder flags)

**Files:**
- Create: `app/services/market_events/query_service.py`
- Test: `tests/services/test_market_events_query_service.py`

`held` / `watched` are returned as `None` for now with a TODO referencing the follow-up issue. The response shape stays stable for `/invest/app` integration later.

- [ ] **Step 8.1: Write failing query-service test**

Create `tests/services/test_market_events_query_service.py`:

```python
"""Read-only query service tests (ROB-128)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest


@pytest.mark.integration
async def test_list_events_for_date_returns_events_with_values(db_session):
    from app.services.market_events.query_service import MarketEventsQueryService
    from app.services.market_events.repository import MarketEventsRepository

    repo = MarketEventsRepository(db_session)
    await repo.upsert_event_with_values(
        {
            "category": "earnings", "market": "us", "symbol": "IONQ",
            "title": "IONQ earnings release", "event_date": date(2026, 5, 7),
            "time_hint": "after_close", "status": "released", "source": "finnhub",
            "fiscal_year": 2026, "fiscal_quarter": 1,
        },
        [{"metric_name": "eps", "period": "Q1-2026",
          "actual": Decimal("-0.38"), "forecast": Decimal("-0.36"), "unit": "USD"}],
    )
    await db_session.commit()

    svc = MarketEventsQueryService(db_session)
    response = await svc.list_for_date(date(2026, 5, 7))
    assert response.date == date(2026, 5, 7)
    assert len(response.events) == 1
    event = response.events[0]
    assert event.symbol == "IONQ"
    assert event.held is None  # placeholder until ROB-XXX follow-up
    assert event.watched is None
    assert len(event.values) == 1
    assert event.values[0].metric_name == "eps"


@pytest.mark.integration
async def test_list_events_filters_by_category_and_market(db_session):
    from app.services.market_events.query_service import MarketEventsQueryService
    from app.services.market_events.repository import MarketEventsRepository

    repo = MarketEventsRepository(db_session)
    await repo.upsert_event_with_values(
        {"category": "earnings", "market": "us", "symbol": "IONQ",
         "event_date": date(2026, 5, 7), "status": "released", "source": "finnhub",
         "fiscal_year": 2026, "fiscal_quarter": 1},
        [],
    )
    await repo.upsert_event_with_values(
        {"category": "disclosure", "market": "kr", "symbol": "00126380",
         "event_date": date(2026, 5, 7), "status": "released", "source": "dart",
         "source_event_id": "20260507000001"},
        [],
    )
    await db_session.commit()

    svc = MarketEventsQueryService(db_session)
    only_kr = await svc.list_for_range(
        date(2026, 5, 7), date(2026, 5, 7), market="kr"
    )
    assert only_kr.count == 1
    assert only_kr.events[0].market == "kr"

    only_earnings = await svc.list_for_range(
        date(2026, 5, 7), date(2026, 5, 7), category="earnings"
    )
    assert only_earnings.count == 1
    assert only_earnings.events[0].category == "earnings"
```

- [ ] **Step 8.2: Run test to confirm it fails**

Run: `uv run pytest tests/services/test_market_events_query_service.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 8.3: Implement the query service**

Create `app/services/market_events/query_service.py`:

```python
"""Read-only query service for market events (ROB-128).

NOTE: held / watched flags currently return None. Joining holdings / watchlist is
deferred to a follow-up — see docs/runbooks/market-events-ingestion.md.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.market_events import MarketEvent
from app.schemas.market_events import (
    MarketEventResponse,
    MarketEventsDayResponse,
    MarketEventsRangeResponse,
    MarketEventValueResponse,
)
from app.services.market_events.taxonomy import (
    validate_category,
    validate_market,
)


class MarketEventsQueryService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def list_for_date(
        self,
        target_date: date,
        *,
        category: str | None = None,
        market: str | None = None,
        source: str | None = None,
    ) -> MarketEventsDayResponse:
        events = await self._query(
            from_date=target_date,
            to_date=target_date,
            category=category,
            market=market,
            source=source,
        )
        return MarketEventsDayResponse(date=target_date, events=events)

    async def list_for_range(
        self,
        from_date: date,
        to_date: date,
        *,
        category: str | None = None,
        market: str | None = None,
        source: str | None = None,
    ) -> MarketEventsRangeResponse:
        if from_date > to_date:
            raise ValueError("from_date must be <= to_date")
        events = await self._query(
            from_date=from_date, to_date=to_date,
            category=category, market=market, source=source,
        )
        return MarketEventsRangeResponse(
            from_date=from_date,
            to_date=to_date,
            count=len(events),
            events=events,
        )

    async def _query(
        self,
        *,
        from_date: date,
        to_date: date,
        category: str | None,
        market: str | None,
        source: str | None,
    ) -> list[MarketEventResponse]:
        if category is not None:
            validate_category(category)
        if market is not None:
            validate_market(market)

        stmt = (
            select(MarketEvent)
            .where(
                MarketEvent.event_date >= from_date,
                MarketEvent.event_date <= to_date,
            )
            .options(selectinload("*"))  # eager-load values via dynamic join below
            .order_by(MarketEvent.event_date.asc(), MarketEvent.symbol.asc())
        )
        if category is not None:
            stmt = stmt.where(MarketEvent.category == category)
        if market is not None:
            stmt = stmt.where(MarketEvent.market == market)
        if source is not None:
            stmt = stmt.where(MarketEvent.source == source)

        rows = (await self.db.execute(stmt)).scalars().all()

        # Load values explicitly per event (simple + safe for the foundation PR).
        from app.models.market_events import MarketEventValue

        out: list[MarketEventResponse] = []
        for row in rows:
            value_rows = (
                await self.db.execute(
                    select(MarketEventValue).where(MarketEventValue.event_id == row.id)
                )
            ).scalars().all()
            out.append(
                MarketEventResponse(
                    category=row.category,
                    market=row.market,
                    country=row.country,
                    symbol=row.symbol,
                    company_name=row.company_name,
                    title=row.title,
                    event_date=row.event_date,
                    release_time_utc=row.release_time_utc,
                    time_hint=row.time_hint,
                    importance=row.importance,
                    status=row.status,
                    source=row.source,
                    source_event_id=row.source_event_id,
                    source_url=row.source_url,
                    fiscal_year=row.fiscal_year,
                    fiscal_quarter=row.fiscal_quarter,
                    held=None,    # TODO: join manual_holdings — see runbook follow-ups
                    watched=None, # TODO: join user_watch_items — see runbook follow-ups
                    values=[
                        MarketEventValueResponse.model_validate(v) for v in value_rows
                    ],
                )
            )
        return out
```

- [ ] **Step 8.4: Run tests**

Run: `uv run pytest tests/services/test_market_events_query_service.py -v`
Expected: 2 PASS.

- [ ] **Step 8.5: Commit**

```bash
git add app/services/market_events/query_service.py tests/services/test_market_events_query_service.py
git commit -m "feat(market_events): add read-only query service with held/watched placeholders (ROB-128)"
```

---

## Task 9: Read-only FastAPI router

**Files:**
- Create: `app/routers/market_events.py`
- Modify: `app/main.py` (one line)
- Test: `tests/test_market_events_router.py`

- [ ] **Step 9.1: Write failing router test**

Create `tests/test_market_events_router.py`:

```python
"""Read-only market events router (ROB-128)."""

from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient


@pytest.mark.integration
def test_get_today_events_returns_empty_when_no_data(db_session, auth_headers):
    """Smoke test: route exists, requires auth, returns empty events."""
    from app.main import app

    with TestClient(app) as client:
        response = client.get(
            "/trading/api/market-events/today?on_date=2026-05-07",
            headers=auth_headers,
        )
        assert response.status_code == 200
        body = response.json()
        assert body["date"] == "2026-05-07"
        assert body["events"] == []


@pytest.mark.integration
def test_get_today_events_unauthorized_without_token():
    from app.main import app

    with TestClient(app) as client:
        response = client.get(
            "/trading/api/market-events/today?on_date=2026-05-07"
        )
        assert response.status_code in (401, 403)


@pytest.mark.integration
def test_get_range_events_validates_date_order(db_session, auth_headers):
    from app.main import app

    with TestClient(app) as client:
        response = client.get(
            "/trading/api/market-events/range?from_date=2026-05-08&to_date=2026-05-07",
            headers=auth_headers,
        )
        assert response.status_code == 400
```

- [ ] **Step 9.2: Run test to confirm router/route missing**

Run: `uv run pytest tests/test_market_events_router.py -v`
Expected: FAIL with 404 (route not registered) or import error.

- [ ] **Step 9.3: Implement the router**

Create `app/routers/market_events.py`:

```python
"""Read-only market events router (ROB-128).

GET only. No mutation. Auth required (matches existing trading/api pattern).
"""

from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.models.trading import User
from app.routers.dependencies import get_authenticated_user
from app.schemas.market_events import (
    MarketEventsDayResponse,
    MarketEventsRangeResponse,
)
from app.services.market_events.query_service import MarketEventsQueryService

router = APIRouter(prefix="/trading", tags=["market-events"])


@router.get(
    "/api/market-events/today",
    response_model=MarketEventsDayResponse,
)
async def get_today_market_events(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_authenticated_user)],
    on_date: Annotated[date | None, Query(description="ISO date; default = today")] = None,
    category: str | None = None,
    market: str | None = None,
    source: str | None = None,
) -> MarketEventsDayResponse:
    target = on_date or date.today()
    svc = MarketEventsQueryService(db)
    try:
        return await svc.list_for_date(
            target, category=category, market=market, source=source
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc


@router.get(
    "/api/market-events/range",
    response_model=MarketEventsRangeResponse,
)
async def get_market_events_range(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_authenticated_user)],
    from_date: Annotated[date, Query(description="ISO start date, inclusive")],
    to_date: Annotated[date, Query(description="ISO end date, inclusive")],
    category: str | None = None,
    market: str | None = None,
    source: str | None = None,
) -> MarketEventsRangeResponse:
    svc = MarketEventsQueryService(db)
    try:
        return await svc.list_for_range(
            from_date, to_date, category=category, market=market, source=source
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
```

- [ ] **Step 9.4: Wire the router into the FastAPI app**

Edit `app/main.py`:

Locate the import block (line 27 in current file) and add `market_events` next to `alpaca_paper_ledger`:

```python
from app.routers import (
    ...
    alpaca_paper_ledger,
    market_events,
    ...
)
```

Locate line 192 (`app.include_router(alpaca_paper_ledger.router)`) and add directly after it:

```python
app.include_router(market_events.router)
```

- [ ] **Step 9.5: Run router tests**

Run: `uv run pytest tests/test_market_events_router.py -v`
Expected: 3 PASS.

- [ ] **Step 9.6: Commit**

```bash
git add app/routers/market_events.py app/main.py tests/test_market_events_router.py
git commit -m "feat(market_events): add read-only GET router for today/range queries (ROB-128)"
```

---

## Task 10: CLI entrypoint (one-day partition loop)

**Files:**
- Create: `scripts/ingest_market_events.py`
- Test: `tests/test_market_events_cli.py`

The CLI splits a date range into one-day partitions and calls the orchestrator per day. Supports `--dry-run` (prints planned partitions without DB writes), default `--source finnhub --category earnings --market us`.

- [ ] **Step 10.1: Write failing CLI test**

Create `tests/test_market_events_cli.py`:

```python
"""CLI tests for scripts/ingest_market_events.py (ROB-128)."""

from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock

import pytest


@pytest.mark.unit
def test_iter_partition_dates_inclusive():
    from scripts.ingest_market_events import iter_partition_dates

    dates = list(iter_partition_dates(date(2026, 5, 7), date(2026, 5, 9)))
    assert dates == [date(2026, 5, 7), date(2026, 5, 8), date(2026, 5, 9)]


@pytest.mark.unit
def test_iter_partition_dates_single_day():
    from scripts.ingest_market_events import iter_partition_dates

    dates = list(iter_partition_dates(date(2026, 5, 7), date(2026, 5, 7)))
    assert dates == [date(2026, 5, 7)]


@pytest.mark.unit
def test_parse_args_defaults():
    from scripts.ingest_market_events import parse_args

    ns = parse_args(["--from-date", "2026-05-07", "--to-date", "2026-05-09"])
    assert ns.source == "finnhub"
    assert ns.category == "earnings"
    assert ns.market == "us"
    assert ns.from_date == date(2026, 5, 7)
    assert ns.to_date == date(2026, 5, 9)
    assert ns.dry_run is False


@pytest.mark.unit
def test_parse_args_rejects_unsupported_source_category_combo():
    import argparse

    from scripts.ingest_market_events import parse_args

    with pytest.raises((SystemExit, argparse.ArgumentTypeError, ValueError)):
        parse_args([
            "--source", "binance", "--category", "earnings", "--market", "us",
            "--from-date", "2026-05-07", "--to-date", "2026-05-07",
        ])


@pytest.mark.integration
async def test_run_ingest_dispatches_per_day(db_session, monkeypatch):
    from scripts import ingest_market_events as cli

    fake = AsyncMock(return_value=type("R", (), {"status": "succeeded", "event_count": 0})())
    monkeypatch.setattr(cli, "ingest_us_earnings_for_date", fake)

    await cli.run_ingest(
        db=db_session,
        source="finnhub",
        category="earnings",
        market="us",
        from_date=date(2026, 5, 7),
        to_date=date(2026, 5, 9),
        dry_run=False,
    )
    assert fake.await_count == 3
```

- [ ] **Step 10.2: Run test to confirm import failure**

Run: `uv run pytest tests/test_market_events_cli.py -v`
Expected: FAIL with `ModuleNotFoundError: scripts.ingest_market_events`.

- [ ] **Step 10.3: Implement the CLI**

Create `scripts/ingest_market_events.py`:

```python
#!/usr/bin/env python3
"""Per-day market events ingestion CLI (ROB-128).

Examples:
    python -m scripts.ingest_market_events \\
        --source finnhub --category earnings --market us \\
        --from-date 2026-05-07 --to-date 2026-05-14

    python -m scripts.ingest_market_events \\
        --source dart --category disclosure --market kr \\
        --from-date 2026-05-07 --to-date 2026-05-07

The command splits the [from_date, to_date] range into single-day partitions and
invokes the ingestion orchestrator per day. Failures are recorded as failed
partitions; subsequent runs only retry failed days when re-invoked with the same
range.

Recommended rolling window for later Prefect schedule:
    today - 7 days through today + 60 days
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from collections.abc import Iterator
from datetime import date, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.monitoring.sentry import capture_exception, init_sentry
from app.services.market_events.ingestion import (
    ingest_kr_disclosures_for_date,
    ingest_us_earnings_for_date,
)

logger = logging.getLogger(__name__)


SUPPORTED = {
    ("finnhub", "earnings", "us"): ingest_us_earnings_for_date,
    ("dart", "disclosure", "kr"): ingest_kr_disclosures_for_date,
}


def iter_partition_dates(from_date: date, to_date: date) -> Iterator[date]:
    if from_date > to_date:
        raise ValueError("from_date must be <= to_date")
    cur = from_date
    while cur <= to_date:
        yield cur
        cur += timedelta(days=1)


def _parse_iso(value: str) -> date:
    return date.fromisoformat(value)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Per-day market events ingestion CLI (ROB-128)."
    )
    parser.add_argument("--source", default="finnhub", choices=["finnhub", "dart"])
    parser.add_argument("--category", default="earnings", choices=["earnings", "disclosure"])
    parser.add_argument("--market", default="us", choices=["us", "kr"])
    parser.add_argument("--from-date", required=True, type=_parse_iso, dest="from_date")
    parser.add_argument("--to-date", required=True, type=_parse_iso, dest="to_date")
    parser.add_argument("--dry-run", action="store_true", dest="dry_run")
    ns = parser.parse_args(argv)

    key = (ns.source, ns.category, ns.market)
    if key not in SUPPORTED:
        parser.error(
            f"unsupported source/category/market combination: {key}. "
            f"supported: {sorted(SUPPORTED.keys())}"
        )
    return ns


async def run_ingest(
    *,
    db: AsyncSession,
    source: str,
    category: str,
    market: str,
    from_date: date,
    to_date: date,
    dry_run: bool,
) -> int:
    fn = SUPPORTED[(source, category, market)]
    succeeded = 0
    failed = 0
    for d in iter_partition_dates(from_date, to_date):
        if dry_run:
            logger.info(
                "[DRY-RUN] would ingest %s/%s/%s for %s", source, category, market, d
            )
            succeeded += 1
            continue
        result = await fn(db, d)
        await db.commit()
        if result.status == "succeeded":
            succeeded += 1
            logger.info(
                "ingested %s events for %s/%s/%s on %s",
                result.event_count, source, category, market, d,
            )
        else:
            failed += 1
            logger.error(
                "ingest failed for %s/%s/%s on %s: %s",
                source, category, market, d, result.error,
            )
    logger.info(
        "ingest complete: succeeded=%s failed=%s range=%s..%s",
        succeeded, failed, from_date, to_date,
    )
    return 0 if failed == 0 else 2


async def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    init_sentry(service_name="market-events-ingest")
    ns = parse_args(argv)

    try:
        async with AsyncSessionLocal() as db:
            return await run_ingest(
                db=db,
                source=ns.source,
                category=ns.category,
                market=ns.market,
                from_date=ns.from_date,
                to_date=ns.to_date,
                dry_run=ns.dry_run,
            )
    except Exception as exc:
        capture_exception(exc, process="ingest_market_events")
        logger.error("ingest_market_events crashed: %s", exc, exc_info=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
```

- [ ] **Step 10.4: Run CLI tests**

Run: `uv run pytest tests/test_market_events_cli.py -v`
Expected: 5 PASS.

- [ ] **Step 10.5: Smoke-run the CLI in dry-run mode**

Run:
```bash
uv run python -m scripts.ingest_market_events \
  --source finnhub --category earnings --market us \
  --from-date 2026-05-07 --to-date 2026-05-09 --dry-run
```
Expected: three "[DRY-RUN] would ingest finnhub/earnings/us for 2026-05-0X" log lines, exit 0, no DB writes.

- [ ] **Step 10.6: Commit**

```bash
git add scripts/ingest_market_events.py tests/test_market_events_cli.py
git commit -m "feat(market_events): add per-day ingestion CLI with dry-run (ROB-128)"
```

---

## Task 11: Runbook documentation

**Files:**
- Create: `docs/runbooks/market-events-ingestion.md`
- Modify: `CLAUDE.md` (add ROB-128 reference paragraph after the existing ROB-104 block)

- [ ] **Step 11.1: Write the runbook**

Create `docs/runbooks/market-events-ingestion.md`:

```markdown
# Market Events Ingestion Foundation (ROB-128)

> Foundation PR. No Prefect schedule, no production backfill, no broker mutation.

## What this is

A per-day, idempotent ingestion pipeline for **market-wide events** (US earnings via
Finnhub, KR DART disclosures, with crypto event taxonomy ready for follow-up sources).
Drives the future "오늘의 이벤트" surface on `/invest/app`.

## Tables

* `market_events` — one row per scheduled / released event. Public schema.
* `market_event_values` — metric-level numeric data (eps, revenue, cpi, …).
* `market_event_ingestion_partitions` — per source/category/market/day state, so failed
  days are visible and retryable rather than silently skipped.

All writes go through `app/services/market_events/repository.py::MarketEventsRepository`.

## Idempotency

* Events with `source_event_id` (e.g. DART `rcept_no`) upsert on
  `(source, category, market, source_event_id)`.
* Events without (e.g. Finnhub earnings rows) upsert on
  `(source, category, market, symbol, event_date, fiscal_year, fiscal_quarter)`.
* Values upsert on `(event_id, metric_name, period)`.

Both keys are partial unique indexes — see migration
`alembic/versions/a7e9c128_add_market_events_tables.py`.

## CLI

```bash
# US earnings, one-day partitions, range looped internally
uv run python -m scripts.ingest_market_events \
  --source finnhub --category earnings --market us \
  --from-date 2026-05-07 --to-date 2026-05-14

# KR disclosures
uv run python -m scripts.ingest_market_events \
  --source dart --category disclosure --market kr \
  --from-date 2026-05-07 --to-date 2026-05-07

# Dry run (prints planned partitions, no DB writes)
uv run python -m scripts.ingest_market_events \
  --source finnhub --category earnings --market us \
  --from-date 2026-05-07 --to-date 2026-05-14 --dry-run
```

Recommended rolling window for the future Prefect schedule:
**today - 7 days through today + 60 days.**

## Read API

* `GET /trading/api/market-events/today?on_date=YYYY-MM-DD&category=&market=&source=`
* `GET /trading/api/market-events/range?from_date=YYYY-MM-DD&to_date=YYYY-MM-DD&...`

Both return `MarketEventResponse` items including `held` and `watched` placeholder
flags (currently always `null` — see follow-ups).

## Env vars

| Var | Purpose | Already in `app/core/config.py`? |
| --- | --- | --- |
| `FINNHUB_API_KEY` | Finnhub earnings calendar | yes |
| `OPENDART_API_KEY` | DART disclosures | yes |

No new env vars introduced by this PR. Tests stub both with `DUMMY_*` values.

## Safety

* `raw_payload_json` columns are passed through `_redact_sensitive_keys` before write.
* No broker / order / watch / scheduling side effects.
* Failures record `partition.status = "failed"` with the error message and increment
  `retry_count`. The partition row is the canonical retry surface.
* Tests use `monkeypatch.setattr(...)` against `_fetch_earnings_calendar_finnhub` and
  the injected `fetch_rows` callable — never live API calls by default.

## Tests

```bash
uv run pytest tests/services/test_market_events_models.py -v
uv run pytest tests/services/test_market_events_taxonomy.py -v
uv run pytest tests/services/test_market_events_schemas.py -v
uv run pytest tests/services/test_market_events_normalizers.py -v
uv run pytest tests/services/test_market_events_repository.py -v
uv run pytest tests/services/test_market_events_ingestion.py -v
uv run pytest tests/services/test_market_events_query_service.py -v
uv run pytest tests/test_market_events_router.py -v
uv run pytest tests/test_market_events_cli.py -v

uv run ruff check .
uv run ruff format --check .
```

The DB-backed integration tests require Postgres at the test `DATABASE_URL`.

## Follow-ups (out of scope for this PR)

1. **Prefect deployment** for the rolling window. The CLI exposes a stable boundary
   (`scripts.ingest_market_events.run_ingest`) for the flow to call.
2. **Holdings / watchlist join** to populate `held` / `watched` flags. Today
   `MarketEventsQueryService` returns `None` for both. The expected surfaces are:
   * `held` ← `manual_holdings.ticker = market_events.symbol` filtered to the
     authenticated user's `broker_account_id`.
   * `watched` ← `user_watch_items.instrument_id` joined via `instruments.symbol =
     market_events.symbol`.
3. **Crypto sources** — the taxonomy already supports `crypto_exchange_notice`,
   `crypto_protocol`, `tokenomics`, `regulatory`. Implement Upbit / Bithumb /
   Binance notice fetchers as additional `ingest_*_for_date` functions and add
   them to `SUPPORTED` in `scripts/ingest_market_events.py`.
4. **Economic calendar** (`category="economic"`) — same shape, different source.
5. **`/invest/app` UI card** consuming the `today` endpoint.

## Handoff (when this PR is opened)

Include in the PR description / Linear comment:

* branch name + PR URL
* `alembic/versions/a7e9c128_add_market_events_tables.py` (migration filename)
* CLI invocation examples (above)
* tests + lint commands run, with output
* whether any live API calls were used (default: no)
* required env vars (above), with values redacted
* production migration / backfill cautions
* exact follow-up tasks for Hermes / Prefect (above)
```

- [ ] **Step 11.2: Add CLAUDE.md reference**

Edit `CLAUDE.md`. After the existing block:

```
### KIS WebSocket Mock Smoke (ROB-104)
...
- **이벤트 태깅**: ...
```

Insert (paste verbatim — DO NOT modify surrounding text):

```markdown

### Market Events Ingestion Foundation (ROB-128)

시장 이벤트 (US earnings, KR DART 공시, 향후 crypto/economic) 수집·저장·조회 foundation.

- **모델**: `app/models/market_events.py` — `MarketEvent`, `MarketEventValue`, `MarketEventIngestionPartition`
- **서비스**: `app/services/market_events/` — `repository`, `ingestion`, `query_service`, `normalizers`, `taxonomy`
- **라우터**: `app/routers/market_events.py` — GET `/trading/api/market-events/today`, `/range` (read-only)
- **CLI**: `scripts/ingest_market_events.py` — `--source finnhub|dart --category earnings|disclosure --market us|kr --from-date --to-date [--dry-run]`
- **런북**: `docs/runbooks/market-events-ingestion.md`

**안전 경계**: read-mostly 마켓 데이터, 브로커/주문/감시 mutation 없음. `raw_payload_json` 은 저장 전 `_redact_sensitive_keys` 적용. 모든 DB 쓰기는 `MarketEventsRepository` 경유. Prefect 배포는 후속 작업.
```

- [ ] **Step 11.3: Commit**

```bash
git add docs/runbooks/market-events-ingestion.md CLAUDE.md
git commit -m "docs(market_events): add runbook + CLAUDE.md reference (ROB-128)"
```

---

## Task 12: Final verification

- [ ] **Step 12.1: Run all market_events tests together**

Run:
```bash
uv run pytest \
  tests/services/test_market_events_models.py \
  tests/services/test_market_events_taxonomy.py \
  tests/services/test_market_events_schemas.py \
  tests/services/test_market_events_normalizers.py \
  tests/services/test_market_events_repository.py \
  tests/services/test_market_events_ingestion.py \
  tests/services/test_market_events_query_service.py \
  tests/test_market_events_router.py \
  tests/test_market_events_cli.py \
  -v
```
Expected: all PASS, ~30 tests total.

- [ ] **Step 12.2: Run lint + format**

Run:
```bash
uv run ruff check .
uv run ruff format --check .
```
Expected: no issues.

- [ ] **Step 12.3: Run typecheck (project convention)**

Run: `make typecheck`
Expected: no new errors introduced by this PR (pre-existing errors are out of scope).

- [ ] **Step 12.4: Run full test suite excluding live integration**

Run: `uv run pytest -m "not live and not slow" -q`
Expected: green (or only pre-existing failures unrelated to this PR — list them in PR description).

- [ ] **Step 12.5: Push branch and open PR**

Use the existing branch `feature/ROB-128-market-events-ingestion-foundation` (or
`mgh332696/rob-128-auto_trader-market-events-ingestion-foundation-for-earnings`
suggested by Linear). PR description must include items from the runbook's
"Handoff" section.

---

## Self-review notes

* Spec coverage: §1 DB foundation → Task 1+2; §2 US Finnhub ingestion → Task 5+7+10;
  §3 DART foundation → Task 5 (normalizer) + Task 7 (`ingest_kr_disclosures_for_date`)
  + Task 7.4 (`fetch_dart_filings_for_date` helper); §4 Crypto taxonomy → Task 3
  (`crypto_exchange_notice`, `crypto_protocol`, `tokenomics`, `regulatory` in
  `CATEGORIES`); §5 Read foundation → Task 8 + Task 9; tests → Tasks 1, 5, 6, 7, 8,
  9, 10; docs → Task 11; non-goals (no Prefect, no broker mutation) preserved
  throughout.
* Acceptance criteria: ✓ DB migration; ✓ stable unique keys / upserts; ✓ one-day
  Finnhub partitions; ✓ partition state per `source/category/market/date`;
  ✓ tests for Finnhub eps+revenue, idempotent re-ingestion, failed partition,
  DART rcept_no mapping, crypto taxonomy presence; ✓ CLI; ✓ read API;
  ✓ no broker mutation; ✓ targeted tests pass; ✓ PR description guidance.
* Held/watched join is intentionally deferred (see runbook follow-ups §2). Schema
  shape is stable so the `/invest/app` UI can land later without breaking changes.
